import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1]
ROOT = ADAPTER.parent
sys.path.insert(0, str(ADAPTER))

import macro
import playback
import prepare


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def build(self, text=b"aaAA!!<>\nsecond line\n"):
        payload = self.folder / "payload.txt"
        payload.write_bytes(text)
        bundle = self.folder / "bundle"
        manifest = prepare.build(payload, bundle)
        return bundle, manifest

    def test_bundle_is_standalone_and_checks_offline(self):
        bundle, manifest = self.build()
        result = subprocess.run([sys.executable, str(bundle / "device.py"), "check"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), manifest["macro"])
        self.assertEqual((bundle / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
        steps = list(macro.iter_steps(bundle / "macro.bin"))
        self.assertEqual(steps[0], (1000, bytes(8), macro.GAMEPAD_NEUTRAL))
        self.assertEqual(steps[-1], (0, bytes(8), macro.GAMEPAD_NEUTRAL))
        profile = json.loads((bundle / "profile.json").read_text())
        self.assertEqual((profile["vid"], profile["pid"]), (0x0F0D, 0x0092))
        self.assertEqual((len(bytes.fromhex(profile["gamepad"])), len(bytes.fromhex(profile["keyboard"]))), (86, 63))

    def test_every_printable_character_has_an_ordered_press(self):
        text = "".join(chr(i) for i in range(32, 127)) + "aaAA!!"
        bundle, _ = self.build((text + "\n").encode())
        pressed, previous = [], 0
        for _, keyboard, _ in macro.iter_steps(bundle / "macro.bin"):
            key = keyboard[2]
            if key and key != previous and key not in (40, 41):
                pressed.append((keyboard[0], key))
            previous = key
        # Standard HID usage values, independent of the Pico symbolic key mapping.
        pairs = {c: (0, 4 + i) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
        pairs.update({c: (2, 4 + i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
        pairs.update({c: (0, 30 + i) for i, c in enumerate("1234567890")})
        pairs.update({c: (2, 30 + i) for i, c in enumerate("!@#$%^&*()")})
        for code, low, high in ((44, " ", " "), (45, "-", "_"), (46, "=", "+"),
                                (47, "[", "{"), (48, "]", "}"), (49, "\\", "|"),
                                (51, ";", ":"), (52, "'", '"'), (53, "`", "~"),
                                (54, ",", "<"), (55, ".", ">"), (56, "/", "?")):
            pairs[low] = (0, code)
            if high != low:
                pairs[high] = (2, code)
        self.assertEqual(pressed, [pairs[c] for c in text])

    def test_invalid_input_does_not_create_bundle(self):
        for data in (b"", b"\n  \n", b"a" * 255, b"abc\x0bdef", b"abc\tdef", "é".encode()):
            with self.subTest(data=data[:20]):
                payload = self.folder / "bad.txt"
                payload.write_bytes(data)
                with self.assertRaises((ValueError, UnicodeError)):
                    prepare.build(payload, self.folder / "rejected")
                self.assertFalse((self.folder / "rejected").exists())

    def test_existing_bundle_is_never_overwritten(self):
        bundle, _ = self.build()
        before = (bundle / "macro.bin").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare.build(self.folder / "payload.txt", bundle)
        self.assertEqual((bundle / "macro.bin").read_bytes(), before)

    def test_c_terminator_inside_payload_comment_does_not_truncate_the_plan(self):
        bundle, manifest = self.build(b'prefix};suffix\nnext line\n')
        steps = list(macro.iter_steps(bundle / "macro.bin"))
        self.assertEqual(steps[-1], (0, bytes(8), macro.GAMEPAD_NEUTRAL))
        self.assertGreater(manifest["macro"]["steps"], 30)

    def test_truncated_and_tampered_macros_are_rejected(self):
        bundle, _ = self.build()
        path = bundle / "macro.bin"
        original = path.read_bytes()
        for data in (original[:-1], original + b"x", b"x" + original[1:],
                     original[:-2] + bytes([original[-2] ^ 1]) + original[-1:]):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                macro.inspect_macro(path)

    def test_invalid_terminal_state_and_zero_holds_are_rejected(self):
        neutral = (1000, bytes(8), macro.GAMEPAD_NEUTRAL)
        end = (0, bytes(8), macro.GAMEPAD_NEUTRAL)
        for steps in ([neutral, neutral], [neutral, end, end]):
            with self.assertRaises(ValueError):
                macro.write_macro(self.folder / "bad.bin", steps)

    def test_decoder_rejects_unknown_operations_instead_of_evaluating_c(self):
        for operation in ("PAD_DPAD(5, 0)", "WAIT(__import__('os'))", "END()\nWAIT(5)"):
            header = "static const MacroStep MACRO_SCRIPT[] = {\n" + operation + ",\n};"
            with self.assertRaises((ValueError, KeyError)):
                prepare.decode_header(header)

    def test_delayed_transport_does_not_shorten_later_holds(self):
        bundle, _ = self.build()
        class DelayedTransport:
            now = 0
            states = []
            holds = []
            def send(self, keyboard, gamepad):
                self.now += 0.017  # time spent waiting for USB writes
                self.states.append((keyboard, gamepad))
            def hold(self, seconds):
                self.now += seconds
                self.holds.append(seconds)
        transport = DelayedTransport()
        playback.replay(bundle / "macro.bin", transport)
        steps = list(macro.iter_steps(bundle / "macro.bin"))
        self.assertEqual(transport.holds, [s[0] / 1000 for s in steps])
        self.assertEqual(transport.states, [(s[1], s[2]) for s in steps])
        self.assertAlmostEqual(transport.now, sum(transport.holds) + len(steps) * 0.017)

    def test_replay_uses_the_validated_snapshot_after_file_replacement(self):
        bundle, _ = self.build()
        path = bundle / "macro.bin"
        snapshot, summary = macro.snapshot_macro(path)
        expected = list(macro.iter_steps(snapshot))
        path.write_bytes(b"replaced while waiting for USB")
        self.assertEqual(list(macro.iter_steps(snapshot)), expected)
        self.assertEqual(summary["steps"], len(expected))
        with self.assertRaises(ValueError):
            macro.inspect_macro(path)

    def test_hid_write_waits_for_transfer_completion_before_returning(self):
        pair = playback.HidPair.__new__(playback.HidPair)
        pair.fds, pair.timeout, pair.last = {"gamepad": 10, "keyboard": 11}, 2, {}
        clock, pending, events = [0.0], [False], []
        def select_write(_read, write, _except, _timeout):
            if pending[0]:
                clock[0] += 0.125
                pending[0] = False
                events.append("completed")
            else:
                events.append("ready")
            return [], write, []
        def submit(_fd, data):
            pending[0] = True
            events.append("queued")
            return len(data)
        with patch.object(playback.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(playback.select, "select", side_effect=select_write), \
             patch.object(playback.os, "write", side_effect=submit):
            pair.send(bytes(8), macro.GAMEPAD_NEUTRAL)
        self.assertEqual(events, ["ready", "queued", "completed"] * 2)
        self.assertEqual(clock[0], 0.25)
        self.assertFalse(pending[0])

    def test_hid_completion_timeout_does_not_report_success(self):
        pair = playback.HidPair.__new__(playback.HidPair)
        pair.fds, pair.timeout = {"gamepad": 10}, 2
        with patch.object(playback.select, "select", side_effect=[([], [10], []), ([], [], [])]), \
             patch.object(playback.os, "write", return_value=8):
            with self.assertRaisesRegex(TimeoutError, "did not complete"):
                pair.write("gamepad", macro.GAMEPAD_NEUTRAL)

    @unittest.skipUnless(shutil.which("g++"), "g++ unavailable; run native Pico parity on Linux")
    def test_native_original_pico_engine_matches_all_linux_records(self):
        text = "".join(chr(i) for i in range(32, 127)) + "aaAA!!\nsecond line"
        bundle, _ = self.build((text + "\n").encode())
        subprocess.run([sys.executable, str(ROOT / "PicoSwitch/tools/payload_to_macro.py"),
                        str(self.folder / "payload.txt"), str(self.folder / "macro_script.h")], check=True)
        for name in ("MacroEngine.h", "SwitchConsts.h"):
            shutil.copyfile(ROOT / "PicoSwitch/include" / name, self.folder / name)
        (self.folder / "pico").mkdir()
        (self.folder / "pico/stdlib.h").write_text("""
#pragma once
#include <cstdint>
#include <cstddef>
inline uint32_t fake_clock = 0;
inline uint64_t get_absolute_time() { return fake_clock; }
inline uint32_t to_ms_since_boot(uint64_t t) { return t; }
constexpr int PICO_DEFAULT_LED_PIN = 0, GPIO_OUT = 1;
inline void gpio_init(int) {}
inline void gpio_set_dir(int, int) {}
inline void gpio_put(int, bool) {}
""")
        (self.folder / "Controller.h").write_text('#pragma once\n#include "SwitchConsts.h"\n'
                                                  'class Controller { public: Controller(int,int) {} };\n')
        (self.folder / "tusb.h").write_text("\n".join(f"#define {key} {value}" for key, value in prepare.KEYS.items() if key != "0"))
        engine = (ROOT / "PicoSwitch/src/macro/MacroEngine.cpp").as_posix()
        (self.folder / "parity.cpp").write_text('''
#include <cstdio>
#include <cassert>
#include "''' + engine + '''"
static void dump(uint32_t hold, const KeyboardReport &kbd, const SwitchUsbReport &pad) {
  std::printf("%u ", hold);
  for (size_t j=0; j<sizeof(kbd); ++j) std::printf("%02x", reinterpret_cast<const unsigned char*>(&kbd)[j]);
  std::printf(" ");
  for (size_t j=0; j<sizeof(pad); ++j) std::printf("%02x", reinterpret_cast<const unsigned char*>(&pad)[j]);
  std::printf("\\n");
}
int main() {
  static_assert(sizeof(KeyboardReport)==8 && sizeof(SwitchUsbReport)==8);
  assert(switch_usb_string_descriptors[0][0] == 0x09);
  MacroEngine engine; engine.init();
  KeyboardReport kbd; SwitchUsbReport pad;
  fake_clock = MACRO_START_DELAY_MS-1;
  engine.getSwitchUsbReport(&pad); engine.getKeyboardReport(&kbd);
  assert(kbd.modifier==0 && kbd.keycode[0]==0 && pad.buttons==0 && pad.hat==8);
  dump(MACRO_START_DELAY_MS, kbd, pad);
  fake_clock = MACRO_START_DELAY_MS;
  for (const MacroStep &step: MACRO_SCRIPT) {
    engine.getSwitchUsbReport(&pad); engine.getKeyboardReport(&kbd);
    dump(step.durationMs, kbd, pad);
    if (step.op == MACRO_OP_END) break;
    fake_clock += step.durationMs;
  }
  return 0;
}
''', encoding="utf-8")
        binary = self.folder / "parity"
        compiled = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(self.folder),
                                   str(self.folder / "parity.cpp"), "-o", str(binary)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        output = subprocess.check_output([str(binary)], text=True)
        expected = [f"{hold} {kbd.hex()} {pad.hex()}" for hold, kbd, pad in macro.iter_steps(bundle / "macro.bin")]
        self.assertEqual(output.splitlines(), expected)


if __name__ == "__main__":
    unittest.main()
