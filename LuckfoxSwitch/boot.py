#!/usr/bin/env python3
"""Luckfox startup: automatic Pico macro on USB configuration, manual Murloc fallback."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from gadget import Gadget, deferred_signals, load_profile, read_text
from macro import GAMEPAD_NEUTRAL, KEYBOARD_NEUTRAL, snapshot_macro
from playback import HidPair, replay

HERE = Path(__file__).resolve().parent
ROOT_MODE = Path("/etc/civswitch/mode")
CARD_DIR = Path("/mnt/sdcard/civswitch")
STATUS = Path("/run/civswitch-status.json")
VENDOR_LOCK = Path("/var/run/.usbdevice.lock")
WORKER_LOCK = Path("/run/civswitch-boot.lock")
CONTROL_LOCK = Path("/run/civswitch-control.lock")
STOP = Path("/run/civswitch-stop")


def marker(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        value = stream.read(129)
    if len(value) > 128:
        raise ValueError("Control marker too long")
    return value.strip().lower()


def configured_mode(root=ROOT_MODE, card=CARD_DIR / "mode.txt"):
    for path in (card, root):
        if path.exists():
            try:
                value = marker(path)
            except (OSError, ValueError, UnicodeError):
                return "murloc"
            return value if value in ("civswitch", "murloc") else "murloc"
    return "murloc"


def status(stage, **details):
    STATUS.write_text(json.dumps({"stage": stage, **details}, indent=2) + "\n")
    print(stage, details, flush=True)


@contextmanager
def lock_file(path, timeout=30):
    import fcntl
    with path.open("a") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"USB owner lock busy: {path}")
                time.sleep(0.1)
        yield


def ensure_module(base=HERE):
    meta = json.loads((base / "module.json").read_text())
    release = os.uname().release
    if release != meta["kernel_release"]:
        raise RuntimeError("Installed HID module was not qualified for this kernel")
    module = Path("/lib/modules") / release / "kernel/drivers/usb/gadget/function/usb_f_hid.ko"
    if hashlib.sha256(module.read_bytes()).hexdigest() != meta["sha256"]:
        raise RuntimeError("Installed HID module checksum differs from its receipt")
    deadline = time.monotonic() + 30
    while not Path("/sys/module/libcomposite").exists():
        if STOP.exists():
            raise InterruptedError("Startup stop requested")
        if time.monotonic() >= deadline:
            raise TimeoutError("Vendor USB stack did not become ready")
        time.sleep(0.2)
    if not Path("/sys/module/usb_f_hid").exists():
        subprocess.run(["insmod", str(module)], check=True)


def interrupt(_signum, _frame):
    raise KeyboardInterrupt()


@contextmanager
def ready_vendor(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if STOP.exists():
            raise InterruptedError("Startup stop requested")
        with lock_file(VENDOR_LOCK, timeout=max(0.1, deadline - time.monotonic())):
            udc = read_text("/sys/kernel/config/usb_gadget/rockchip/UDC")
            if udc:
                yield udc
                return
        # Release the vendor lock so its delayed startup can finish binding.
        time.sleep(0.2)
    raise TimeoutError("Vendor gadget did not become ready")


def cleanup(transport, gadget):
    with deferred_signals():
        try:
            if transport is not None:
                transport.close()
        finally:
            gadget.close()


def run_boot(test_idle=0):
    if configured_mode() == "murloc" and not test_idle:
        status("murloc")
        return
    # Production follows Pico: one automatic sequence after USB enumeration.
    # Only the explicitly bounded diagnostic command suppresses playback.
    pending = not test_idle
    data = snapshot_macro(HERE / "macro.bin")[0] if pending else None
    profile = load_profile(HERE / "profile.json")
    ensure_module()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupt)
    # The stock daemon uses this lock for every start/update/stop operation.
    # Hold it before snapshotting ownership, until after restoring its gadget.
    with ready_vendor() as udc, lock_file(Path("/run/civswitch-lyra.lock")):
        gadget = Gadget(profile, udc, "rockchip")
        transport = None
        deadline = time.monotonic() + test_idle if test_idle else None
        try:
            gadget.setup()
            status("waiting_for_usb", playback_pending=pending, controller=udc)
            while test_idle or configured_mode() == "civswitch":
                if STOP.exists():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                state = read_text(gadget.udcs / udc / "state")
                if state != "configured":
                    if transport is not None:
                        transport.close()
                        transport = None
                    time.sleep(0.2)
                    continue
                if transport is None:
                    transport = HidPair(gadget.nodes(), echo_gamepad=not test_idle)
                    transport.send(KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL)
                    status("usb_configured", playback_pending=pending)
                if pending:
                    pending = False
                    status("macro_started")
                    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("Macro timeout")))
                    signal.alarm(1800)
                    try:
                        replay(data, transport, cancelled=lambda: STOP.exists() or configured_mode() != "civswitch")
                    finally:
                        signal.alarm(0)
                    status("macro_finished")
                transport.hold(0.5)
        finally:
            signal.alarm(0)
            cleanup(transport, gadget)
            status("murloc_restored")


def startup(test_idle=0):
    # Serialize commands, and own the worker before clearing a previous stop.
    # A duplicate start must never erase cancellation of the active worker.
    with ExitStack() as worker:
        with lock_file(CONTROL_LOCK):
            worker.enter_context(lock_file(WORKER_LOCK, timeout=1))
            STOP.unlink(missing_ok=True)
        if not STOP.exists():
            run_boot(test_idle)


def stop_worker():
    with lock_file(CONTROL_LOCK):
        STOP.write_text("stop\n")
        with lock_file(WORKER_LOCK, timeout=20):
            if read_text("/sys/kernel/config/usb_gadget/civswitch_lyra/UDC"):
                raise RuntimeError("Worker exited without restoring USB; use recorded recovery")
    print("CivSwitch worker stopped and its USB ownership released")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("boot")
    boot.add_argument("--test-idle", type=int, default=0, choices=range(0, 61), metavar="0..60")
    mode = sub.add_parser("mode", help="choose the USB personality; murloc also stops playback")
    mode.add_argument("value", choices=("civswitch", "murloc"))
    sub.add_parser("status")
    sub.add_parser("stop", help="stop this session and wait for USB cleanup; keep the startup mode")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "Startup control requires root\n")
    if args.command == "status":
        print(STATUS.read_text() if STATUS.exists() else "No startup status")
        return
    if args.command == "stop":
        stop_worker()
        return
    if args.command == "mode":
        ROOT_MODE.parent.mkdir(parents=True, exist_ok=True)
        ROOT_MODE.write_text(args.value + "\n")
        if CARD_DIR.parent.is_mount() or Path("/mnt/sdcard").is_mount():
            CARD_DIR.mkdir(exist_ok=True)
            (CARD_DIR / "mode.txt").write_text(args.value + "\n")
        os.sync()
        print("Startup USB mode:", args.value)
    else:
        try:
            startup(args.test_idle)
        except InterruptedError:
            status("stopped")
        except BaseException as exc:
            status("error", reason=str(exc))
            raise


if __name__ == "__main__":
    main()
