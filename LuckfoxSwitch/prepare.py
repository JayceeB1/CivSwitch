#!/usr/bin/env python3
"""Build a Linux replay bundle using the unchanged Pico macro generator."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from macro import GAMEPAD_NEUTRAL, KEYBOARD_NEUTRAL, write_macro

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KEYS = {"0": 0, "KEYBOARD_MODIFIER_LEFTSHIFT": 2}
KEYS.update({f"HID_KEY_{c}": 4 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
KEYS.update({f"HID_KEY_{c}": 30 + i for i, c in enumerate("1234567890")})
KEYS.update({f"HID_KEY_{name}": code for name, code in {
    "ENTER": 40, "ESCAPE": 41, "TAB": 43, "SPACE": 44, "MINUS": 45,
    "EQUAL": 46, "BRACKET_LEFT": 47, "BRACKET_RIGHT": 48, "BACKSLASH": 49,
    "SEMICOLON": 51, "APOSTROPHE": 52, "GRAVE": 53, "COMMA": 54,
    "PERIOD": 55, "SLASH": 56}.items()})


def array_bytes(source, name):
    match = re.search(r"\b" + re.escape(name) + r"\[\]\s*=\s*\{(.*?)\};", source, re.S)
    if not match:
        raise ValueError(f"Pico array not found: {name}")
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", match[1], flags=re.S)
    tokens = [x.strip() for x in text.split(",") if x.strip()]
    if any(not re.fullmatch(r"0[xX][0-9a-fA-F]+|[0-9]+", x) for x in tokens):
        raise ValueError(f"Unsupported expression in {name}")
    return bytes(int(x, 0) for x in tokens)


def pico_profile(root=ROOT):
    source = (root / "PicoSwitch/include/SwitchConsts.h").read_text(encoding="utf-8")
    device = array_bytes(source, "switch_usb_device_descriptor")
    return {
        "schema": 1, "vid": int.from_bytes(device[8:10], "little"),
        "pid": int.from_bytes(device[10:12], "little"),
        "bcd_usb": int.from_bytes(device[2:4], "little"),
        "bcd_device": int.from_bytes(device[12:14], "little"),
        "manufacturer": re.search(r'switch_usb_string_manufacturer\[\]\s*=\s*"([^"]+)"', source)[1],
        "product": re.search(r'switch_usb_string_product\[\]\s*=\s*"([^"]+)"', source)[1],
        "gamepad": array_bytes(source, "switch_usb_report_descriptor").hex(),
        "keyboard": array_bytes(source, "keyboard_usb_report_descriptor").hex(),
    }


def decode_header(header, start_delay=1000):
    """Strictly decode the small macro DSL; never evaluate generated C expressions."""
    header_lines = header.splitlines()
    start = header_lines.index("static const MacroStep MACRO_SCRIPT[] = {") + 1
    # Payloads occur in generated // comments and may themselves contain `};`.
    # Only a complete C initializer terminator line ends the macro body.
    stop = next((i for i in range(start, len(header_lines)) if header_lines[i].strip() == "};"), None)
    if stop is None:
        raise ValueError("Pico macro initializer is not terminated")
    steps = [(start_delay, KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL)]
    ended = False
    for line in header_lines[start:stop]:
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        match = re.fullmatch(r"([A-Z_]+)\((.*?)\),", line)
        if not match or ended:
            raise ValueError(f"Unsupported Pico macro line: {line[:80]}")
        op, args = match.groups()
        args = [x.strip() for x in args.split(",")] if args else []
        keyboard, gamepad = KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL
        if op == "END" and not args:
            duration, ended = 0, True
        elif op == "WAIT" and len(args) == 1:
            duration = int(args[0])
        elif op == "KEYS" and len(args) == 8:
            duration = int(args[0])
            keyboard = bytes([KEYS[args[1]], 0] + [KEYS[x] for x in args[2:]])
        elif op == "PAD_BTN" and len(args) == 2 and args[1] == "SWITCH_USB_MASK_A":
            duration = int(args[0])
            gamepad = bytes.fromhex("0400088080808000")
        else:
            raise ValueError(f"Unsupported Pico operation: {op}")
        steps.append((duration, keyboard, gamepad))
    if not ended:
        raise ValueError("Pico macro has no END")
    return steps


def build(payload, output, root=ROOT):
    payload, output = Path(payload).resolve(), Path(output).resolve()
    raw = payload.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("Payload exceeds 256 KiB")
    if any(byte < 32 and byte not in (10, 13) for byte in raw):
        raise ValueError("Payload contains unsupported control characters")
    text = raw.decode("ascii")
    lines = text.splitlines()
    if not any(x.strip() for x in lines) or len(lines) > 1024:
        raise ValueError("Payload must contain 1..1024 lines")
    for line in lines:
        if len(line) > 254 or any(ord(c) < 32 or ord(c) > 126 for c in line):
            raise ValueError("Payload must use printable US ASCII, at most 254 characters per line")
    if output.exists():
        raise ValueError("Output already exists; select a new bundle directory")
    engine = (root / "PicoSwitch/include/MacroEngine.h").read_text(encoding="utf-8")
    delay = int(re.search(r"#define MACRO_START_DELAY_MS\s+(\d+)", engine)[1])
    generator = root / "PicoSwitch/tools/payload_to_macro.py"
    with tempfile.TemporaryDirectory() as tmp:
        header_path = Path(tmp) / "macro_script.h"
        subprocess.run([sys.executable, str(generator), str(payload), str(header_path)], check=True)
        steps = decode_header(header_path.read_text(encoding="utf-8"), delay)
    profile = pico_profile(root)
    # Create only a new, task-owned directory after all input/source validation.
    output.mkdir(parents=True)
    summary = write_macro(output / "macro.bin", steps)
    (output / "profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    sources = ["PicoSwitch/tools/payload_to_macro.py", "PicoSwitch/include/MacroEngine.h",
               "PicoSwitch/include/SwitchConsts.h", "PicoSwitch/src/macro/MacroEngine.cpp"]
    manifest = {"schema": 1, "payload_sha256": hashlib.sha256(raw).hexdigest(),
                "macro": summary, "source_sha256": {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for name in ("macro.py", "playback.py", "gadget.py", "device.py", "probe.py", "README.md", "ANALYSIS.md", "kernel-hid.config"):
        shutil.copyfile(HERE / name, output / name)
    shutil.copyfile(root / "LICENSE", output / "LICENSE")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path, help="payload.txt produced by PicoSwitch/compress.js")
    parser.add_argument("output", type=Path, help="new standalone directory for the Luckfox")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.payload, args.output), indent=2))
    except (ValueError, KeyError, IndexError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"prepare: {exc}\n")


if __name__ == "__main__":
    main()
