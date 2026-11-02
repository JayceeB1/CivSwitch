#!/usr/bin/env python3
"""Standalone read-only USB inventory; safe to pipe to python3 on a board."""
import gzip
import json
import platform
from pathlib import Path


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def probe():
    udcs = Path("/sys/class/udc")
    gadgets = Path("/sys/kernel/config/usb_gadget")
    release = platform.release()
    config = None
    try:
        with gzip.open("/proc/config.gz", "rt") as stream:
            config = stream.read()
    except OSError:
        config = read_text(Path("/boot") / ("config-" + release))
    symbols = read_text("/proc/kallsyms") or ""
    return {
        "model": (read_text("/proc/device-tree/model") or "unknown").rstrip("\0"),
        "kernel": release, "machine": platform.machine(), "python": platform.python_version(),
        "configfs": gadgets.is_dir(),
        "controllers": [{"name": p.name, "state": read_text(p / "state"),
                         "function": read_text(p / "function")} for p in udcs.iterdir()] if udcs.exists() else [],
        "gadgets": [{"name": p.name, "udc": read_text(p / "UDC"),
                     "functions": [f.name for f in (p / "functions").iterdir()]}
                    for p in gadgets.iterdir()] if gadgets.exists() else [],
        "hid_gadget": {
            "loaded_module": Path("/sys/module/usb_f_hid").exists(),
            "kernel_symbols_seen": any("hidg_alloc" in x for x in symbols.splitlines()),
            "module_files": [str(p) for p in (Path("/lib/modules") / release).glob("**/usb_f_hid.ko*")],
            "config": [x for x in config.splitlines() if x.startswith("CONFIG_USB_CONFIGFS_F_HID=")
                       or x.startswith("CONFIG_USB_F_HID=")] if config else None,
        },
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
