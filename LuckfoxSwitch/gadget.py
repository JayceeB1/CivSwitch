"""Transactional configfs gadget ownership; never edits the vendor gadget's functions."""
import json
import os
import re
import stat
import signal
from contextlib import contextmanager
from pathlib import Path
from probe import read_text

NAME = "civswitch_lyra"


@contextmanager
def deferred_signals():
    """Allow one cleanup to finish before delivering a pending termination signal."""
    previous = None
    if hasattr(signal, "pthread_sigmask"):
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGALRM))
    try:
        yield
    finally:
        if previous is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def load_profile(path):
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    if profile.get("schema") != 1:
        raise ValueError("Unknown USB profile schema")
    for key in ("vid", "pid", "bcd_usb", "bcd_device"):
        if type(profile.get(key)) is not int or not 0 <= profile[key] <= 65535:
            raise ValueError(f"Invalid USB profile field: {key}")
    for key in ("gamepad", "keyboard"):
        data = bytes.fromhex(profile[key])
        if not 1 <= len(data) <= 4096:
            raise ValueError(f"Invalid {key} descriptor")
    for key in ("manufacturer", "product"):
        if not isinstance(profile.get(key), str) or not 1 <= len(profile[key]) <= 126:
            raise ValueError(f"Invalid USB string: {key}")
    return profile


class Gadget:
    def __init__(self, profile, udc, replace=None, *,
                 config_root=Path("/sys/kernel/config/usb_gadget"),
                 udc_root=Path("/sys/class/udc"), dev_root=Path("/dev"),
                 state_file=Path("/run/civswitch-lyra.json")):
        self.profile, self.udc, self.replace = profile, udc, replace
        if not isinstance(udc, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", udc) or udc in (".", ".."):
            raise ValueError("Invalid controller name")
        if replace is not None and (not isinstance(replace, str)
                                   or not re.fullmatch(r"[A-Za-z0-9_.-]+", replace)
                                   or replace in (".", "..", NAME)):
            raise ValueError("Invalid replacement gadget name")
        self.root, self.udcs, self.devs = Path(config_root), Path(udc_root), Path(dev_root)
        self.path, self.state_file = self.root / NAME, Path(state_file)
        self.created = []
        self.old_unbound = False
        self.owns_state = False
        self.owns_gadget = False

    def write(self, path, value):
        path = Path(path)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(str(value) + "\n", encoding="ascii")

    def mkdir(self, path):
        with deferred_signals():
            Path(path).mkdir()
            self.created.append(Path(path))
            if Path(path) == self.path:
                self.owns_gadget = True

    def link(self, target, path):
        with deferred_signals():
            Path(path).symlink_to(target)
            self.created.append(Path(path))

    def remove(self, path):
        if path.is_symlink():
            path.unlink()
        else:
            path.rmdir()  # configfs attributes disappear with their owning directory

    def bindings(self):
        return {p.name: read_text(p / "UDC") for p in self.root.iterdir() if p.is_dir()}

    def check_controller(self):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", self.udc) or not (self.udcs / self.udc).is_dir():
            raise ValueError("Select an exact controller name reported by probe")
        if not self.root.is_dir():
            raise RuntimeError("USB configfs is unavailable; check the kernel and its mount")
        if self.replace is not None and (not re.fullmatch(r"[A-Za-z0-9_.-]+", self.replace)
                                         or self.replace in (".", "..", NAME)):
            raise ValueError("Invalid replacement gadget name")
        bindings = self.bindings()
        bound = [name for name, udc in bindings.items() if udc == self.udc]
        function = read_text(self.udcs / self.udc / "function")
        if self.replace is None:
            if bound or function is None or function:
                raise RuntimeError("Controller busy or ownership unknown; no USB state changed")
        elif bound != [self.replace]:
            raise RuntimeError("The selected vendor gadget does not exclusively own this controller")

    def save_state(self):
        state = {"schema": 1, "udc": self.udc, "original": self.replace,
                 "vid": self.profile["vid"], "pid": self.profile["pid"], "phase": "creating"}
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.owns_state = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def mark_prepared(self):
        state = json.loads(self.state_file.read_text())
        state["phase"] = "prepared"
        temporary = self.state_file.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
        finally:
            temporary.unlink(missing_ok=True)

    def setup(self):
        self.check_controller()
        if self.path.exists():
            raise RuntimeError("CivSwitch gadget already exists; use recover for an owned interrupted session")
        self.save_state()
        try:
            self.mkdir(self.path)
            self.owns_gadget = True
            for key, field in (("idVendor", "vid"), ("idProduct", "pid"),
                               ("bcdUSB", "bcd_usb"), ("bcdDevice", "bcd_device")):
                self.write(self.path / key, f"0x{self.profile[field]:04x}")
            for key in ("bDeviceClass", "bDeviceSubClass", "bDeviceProtocol"):
                self.write(self.path / key, "0")
            if (self.path / "max_speed").exists():
                self.write(self.path / "max_speed", "full-speed")
            self.mkdir(self.path / "strings/0x409")
            for key in ("manufacturer", "product"):
                self.write(self.path / "strings/0x409" / key, self.profile[key])
            self.mkdir(self.path / "configs/c.1")
            self.write(self.path / "configs/c.1/MaxPower", "500")
            self.write(self.path / "configs/c.1/bmAttributes", "0x80")
            # Order is part of the Switch profile: gamepad interface 0, keyboard 1.
            # Create BOTH functions before detaching RNDIS, so a missing f_hid is harmless.
            for name, subclass, protocol in (("gamepad", 0, 0), ("keyboard", 1, 1)):
                function = self.path / "functions" / f"hid.{name}"
                try:
                    self.mkdir(function)
                except OSError as exc:
                    raise RuntimeError("Cannot create HID gadget function; check CONFIG_USB_CONFIGFS_F_HID / usb_f_hid") from exc
                for key, value in (("subclass", subclass), ("protocol", protocol), ("report_length", 8)):
                    self.write(function / key, value)
                self.write(function / "report_desc", bytes.fromhex(self.profile[name]))
                self.link(function, self.path / "configs/c.1" / f"hid.{name}")
            # Recheck ownership immediately before the only vendor mutation.
            self.check_controller()
            self.mark_prepared()
            if self.replace:
                # Record intent BEFORE the write: a signal can arrive just
                # after the kernel detached USB. Cleanup reconciles real state.
                self.old_unbound = True
                self.write(self.root / self.replace / "UDC", "")
            self.write(self.path / "UDC", self.udc)
        except BaseException:
            self.close()
            raise

    def nodes(self):
        result = {}
        for name in ("gamepad", "keyboard"):
            device = read_text(self.path / "functions" / f"hid.{name}" / "dev")
            if not device or not re.fullmatch(r"[0-9]+:[0-9]+", device):
                raise RuntimeError("Kernel does not expose HID major/minor mapping")
            major, minor = map(int, device.split(":"))
            matches = []
            for node in self.devs.glob("hidg*"):
                info = node.stat()
                if stat.S_ISCHR(info.st_mode) and (os.major(info.st_rdev), os.minor(info.st_rdev)) == (major, minor):
                    matches.append(node)
            if len(matches) != 1:
                raise RuntimeError(f"Cannot uniquely locate the {name} HID character device")
            result[name] = matches[0]
        return result

    def close(self):
        with deferred_signals():
            self._close()

    def _close(self):
        errors = []
        if self.owns_gadget and read_text(self.path / "UDC") not in (None, "", self.udc):
            raise RuntimeError("USB cleanup incomplete: gadget controller changed; retain the recovery receipt")
        if self.owns_gadget and self.path.exists() and read_text(self.path / "UDC"):
            try:
                self.write(self.path / "UDC", "")
            except OSError as exc:
                errors.append(str(exc))
        remaining = []
        for path in reversed(self.created):
            try:
                if path.exists() or path.is_symlink():
                    self.remove(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                remaining.append(path)
        self.created = list(reversed(remaining))
        if self.old_unbound:
            try:
                bound = [name for name, udc in self.bindings().items() if udc == self.udc]
                current = read_text(self.root / self.replace / "UDC")
                if current == self.udc:
                    self.old_unbound = False
                elif not bound and current == "" and read_text(self.udcs / self.udc / "function") == "":
                    self.write(self.root / self.replace / "UDC", self.udc)
                    self.old_unbound = False
                else:
                    errors.append("Controller ownership changed; vendor restoration requires inspection")
            except OSError as exc:
                errors.append(f"Vendor restoration: {exc}")
        if self.owns_gadget and self.path.exists() and not errors:
            errors.append("Owned gadget still exists")
        if not errors and not self.created and not self.old_unbound and self.owns_state:
            self.state_file.unlink()
            self.owns_state = False
        if errors:
            raise RuntimeError("USB cleanup incomplete; retain the state file and use recover: " + "; ".join(errors))


def recover(profile, **paths):
    state_file = paths.get("state_file", Path("/run/civswitch-lyra.json"))
    state = json.loads(Path(state_file).read_text())
    if state.get("schema") != 1 or (state.get("vid"), state.get("pid")) != (profile["vid"], profile["pid"]):
        raise RuntimeError("Recovery state does not match this USB profile")
    gadget = Gadget(profile, state["udc"], state["original"], **paths)
    if gadget.path.exists():
        creating = state.get("phase") == "creating"
        for attribute, key in (("idVendor", "vid"), ("idProduct", "pid")):
            allowed = {f"0x{profile[key]:04x}"}
            if creating:
                allowed.update((None, "", "0x0000"))
            if read_text(gadget.path / attribute) not in allowed:
                raise RuntimeError("Existing gadget identity differs; refusing recovery")
        expected = {"hid.gamepad", "hid.keyboard"}
        if not {p.name for p in (gadget.path / "functions").iterdir()} <= expected:
            raise RuntimeError("Foreign functions found; refusing recovery")
        if read_text(gadget.path / "UDC") not in ((None, "", gadget.udc) if creating else ("", gadget.udc)):
            raise RuntimeError("Gadget controller changed; refusing recovery")
        for name in ("gamepad", "keyboard"):
            function = gadget.path / "functions" / f"hid.{name}"
            if function.exists():
                descriptor = function / "report_desc"
                actual = descriptor.read_bytes() if descriptor.exists() else b""
                allowed = (b"", bytes.fromhex(profile[name])) if creating else (bytes.fromhex(profile[name]),)
                if actual not in allowed:
                    raise RuntimeError("HID descriptor changed; refusing recovery")
        # Recreate only the cleanup inventory; never use paths from the state file.
        candidates = [gadget.path, gadget.path / "strings/0x409", gadget.path / "configs/c.1"]
        for name in ("gamepad", "keyboard"):
            candidates += [gadget.path / "functions" / f"hid.{name}",
                           gadget.path / "configs/c.1" / f"hid.{name}"]
        gadget.created = [p for p in candidates if p.exists() or p.is_symlink()]
        gadget.owns_gadget = True
    gadget.old_unbound, gadget.owns_state = bool(gadget.replace), True
    gadget.close()
