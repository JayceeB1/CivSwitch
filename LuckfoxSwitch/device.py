#!/usr/bin/env python3
"""Luckfox USB HID adapter: read-only probe, offline check, explicit replay session."""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from gadget import Gadget, load_profile, read_text, recover
from macro import inspect_macro, snapshot_macro
from playback import HidPair, replay
from probe import probe

HERE = Path(__file__).resolve().parent


def wait_state(path, predicate, deadline):
    while time.monotonic() < deadline:
        state = read_text(path)
        if state is None:
            raise RuntimeError("Controller state is unreadable")
        if predicate(state):
            return
        time.sleep(0.1)
    raise TimeoutError("USB connection wait expired")


def wait_connection(gadget, mode, deadline):
    path = gadget.udcs / gadget.udc / "state"
    if mode == "reconnect":
        print("Waiting for initial USB enumeration, then unplug/replug with continuous board power.", flush=True)
        wait_state(path, lambda state: state == "configured", deadline)
        wait_state(path, lambda state: state != "configured", deadline)
    print("Waiting for the selected USB host to configure both HID interfaces.", flush=True)
    wait_state(path, lambda state: state == "configured", deadline)


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"Signal {signum}")


def expired(_signum, _frame):
    raise TimeoutError("Session timeout; restoring the original USB gadget")


def session(args, profile):
    import fcntl  # Linux only; probe and offline validation remain portable.

    if os.geteuid() != 0:
        raise RuntimeError("USB setup requires root")
    macro_data, summary = snapshot_macro(args.macro)
    if args.timeout < summary["duration_ms"] / 1000 + 5:
        raise ValueError("Timeout is shorter than the macro plus connection margin")
    with Path("/run/civswitch-lyra.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gadget = Gadget(profile, args.udc, args.replace_gadget)
        transport = None
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, interrupted)
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(args.timeout)
        try:
            gadget.setup()
            print("CivSwitch USB session created. The original gadget is retained for restoration.", flush=True)
            wait_connection(gadget, args.connection, time.monotonic() + args.timeout)
            transport = HidPair(gadget.nodes())
            replay(macro_data, transport)
            print("Macro finished; restoring USB.", flush=True)
        finally:
            # A second timeout must not interrupt restoration.
            signal.alarm(0)
            if transport is not None:
                errors = transport.close()
                for error in errors:
                    print("HID release: " + error, file=sys.stderr)
            gadget.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="read-only USB/kernel inventory; no modprobe or configfs writes")
    check = sub.add_parser("check", help="validate a macro offline; no USB access")
    check.add_argument("macro", type=Path, nargs="?", default=HERE / "macro.bin")
    run = sub.add_parser("run", help="explicit, temporary USB session and macro replay")
    run.add_argument("--udc", required=True, help="exact controller from probe")
    run.add_argument("--replace-gadget", help="exact existing gadget to temporarily detach, e.g. rockchip")
    run.add_argument("--connection", choices=("reconnect", "ready"), default="reconnect",
                     help="reconnect requires a cable cycle; ready is for a deliberately selected host")
    run.add_argument("--timeout", type=int, choices=range(10, 3601), default=1800, metavar="10..3600")
    run.add_argument("--macro", type=Path, default=HERE / "macro.bin")
    run.add_argument("--profile", type=Path, default=HERE / "profile.json")
    restore = sub.add_parser("recover", help="recover this adapter's recorded interrupted USB session")
    restore.add_argument("--profile", type=Path, default=HERE / "profile.json")
    args = parser.parse_args()
    try:
        if args.command == "probe":
            print(json.dumps(probe(), indent=2))
        elif args.command == "check":
            print(json.dumps(inspect_macro(args.macro), indent=2))
        elif args.command == "run":
            session(args, load_profile(args.profile))
        else:
            import fcntl

            if os.geteuid() != 0:
                raise RuntimeError("Recovery requires root")
            with Path("/run/civswitch-lyra.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                recover(load_profile(args.profile))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, f"LuckfoxSwitch: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "LuckfoxSwitch: session interrupted; restoration attempted\n")


if __name__ == "__main__":
    main()
