import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import device
import gadget
import prepare


class FakeGadget(gadget.Gadget):
    """Configfs filesystem model; failures exercise the real transaction code."""
    fail_hid = False
    fail_bind = False
    fail_remove = None
    interrupt_unbind = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes = []

    def mkdir(self, path):
        if self.fail_hid and path.name.startswith("hid."):
            raise FileNotFoundError("hid function unavailable")
        super().mkdir(path)
        if path == self.path:
            for name in ("functions", "configs", "strings"):
                (path / name).mkdir()
            (path / "UDC").write_text("")
            (path / "max_speed").write_text("")
        elif path.name == "c.1":
            (path / "strings").mkdir()

    def write(self, path, value):
        self.writes.append((path, value))
        if self.fail_bind and path == self.path / "UDC" and value:
            raise OSError("simulated controller bind failure")
        super().write(path, value)
        if path.name == "UDC":
            function = self.udcs / self.udc / "function"
            if value:
                function.write_text(path.parent.name)
            elif gadget.read_text(function) == path.parent.name:
                function.write_text("")
            if self.interrupt_unbind and path == self.root / "rockchip/UDC" and not value:
                raise KeyboardInterrupt("after real unbind")

    def remove(self, path):
        if path == self.fail_remove:
            raise OSError("simulated persistent cleanup failure")
        if path.parent.name == "functions":
            for link in (self.path / "configs/c.1").glob("hid.*"):
                if link.is_symlink() and link.resolve() == path:
                    raise OSError("linked function is busy")
        if path == self.path:
            for name in ("strings", "functions", "configs"):
                if any((path / name).iterdir()):
                    raise OSError("gadget still has children")
        # Real configfs automatically removes virtual attributes/default groups.
        if not path.is_symlink():
            for child in path.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
                elif child.name in ("strings", "functions", "configs") and child.is_dir():
                    child.rmdir()
        super().remove(path)


class GadgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.config, self.udcs = self.folder / "config", self.folder / "udcs"
        self.config.mkdir()
        (self.udcs / "ff740000.usb").mkdir(parents=True)
        (self.udcs / "ff740000.usb/function").write_text("rockchip")
        vendor = self.config / "rockchip"
        (vendor / "functions/ffs.adb").mkdir(parents=True)
        (vendor / "functions/rndis.0").mkdir()
        (vendor / "UDC").write_text("ff740000.usb\n")
        (vendor / "idVendor").write_text("0x2207\n")
        self.profile = prepare.pico_profile()
        self.paths = {"config_root": self.config, "udc_root": self.udcs,
                      "state_file": self.folder / "state.json"}

    def make(self, replace="rockchip"):
        return FakeGadget(self.profile, "ff740000.usb", replace, **self.paths)

    def assert_vendor_preserved(self):
        self.assertEqual(gadget.read_text(self.config / "rockchip/UDC"), "ff740000.usb")
        self.assertEqual(gadget.read_text(self.config / "rockchip/idVendor"), "0x2207")
        self.assertEqual(sorted(p.name for p in (self.config / "rockchip/functions").iterdir()), ["ffs.adb", "rndis.0"])
        self.assertEqual(gadget.read_text(self.udcs / "ff740000.usb/function"), "rockchip")
        self.assertFalse((self.config / gadget.NAME).exists())

    def test_busy_controller_requires_exact_explicit_replacement(self):
        for replacement in (None, "other"):
            with self.subTest(replacement=replacement):
                candidate = self.make(replacement)
                with self.assertRaises(RuntimeError):
                    candidate.setup()
                candidate.close()
                self.assertEqual(candidate.writes, [])
                self.assert_vendor_preserved()

    def test_missing_hid_fails_before_vendor_unbind(self):
        candidate = self.make()
        candidate.fail_hid = True
        with self.assertRaisesRegex(RuntimeError, "CONFIG_USB_CONFIGFS_F_HID"):
            candidate.setup()
        self.assertNotIn(self.config / "rockchip/UDC", [path for path, _ in candidate.writes])
        self.assert_vendor_preserved()
        self.assertFalse(self.paths["state_file"].exists())

    def test_bind_failure_rolls_back_and_restores_vendor(self):
        candidate = self.make()
        candidate.fail_bind = True
        with self.assertRaisesRegex(OSError, "bind failure"):
            candidate.setup()
        self.assert_vendor_preserved()
        self.assertFalse(self.paths["state_file"].exists())

    def test_signal_just_after_vendor_unbind_restores_the_vendor(self):
        candidate = self.make()
        candidate.interrupt_unbind = True
        with self.assertRaises(KeyboardInterrupt):
            candidate.setup()
        self.assert_vendor_preserved()
        self.assertFalse(self.paths["state_file"].exists())

    def test_repeated_failed_cleanup_retains_paths_and_receipt(self):
        candidate = self.make()
        candidate.setup()
        candidate.fail_remove = candidate.path / "configs/c.1/hid.keyboard"
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                candidate.close()
            self.assertTrue(self.paths["state_file"].exists())
            self.assertIn(candidate.fail_remove, candidate.created)
            self.assertTrue(candidate.path.exists())
        candidate.fail_remove = None
        candidate.close()
        self.assert_vendor_preserved()
        self.assertFalse(self.paths["state_file"].exists())

    def test_recovery_accepts_recorded_incomplete_creation_prefixes(self):
        for stage in ("root", "vendor_id", "empty_function"):
            with self.subTest(stage=stage):
                candidate = self.make()
                candidate.save_state()
                candidate.mkdir(candidate.path)
                if stage != "root":
                    candidate.write(candidate.path / "idVendor", "0x0f0d")
                if stage == "empty_function":
                    candidate.mkdir(candidate.path / "functions/hid.gamepad")
                with patch.object(gadget, "Gadget", FakeGadget):
                    gadget.recover(self.profile, **self.paths)
                self.assert_vendor_preserved()
                self.assertFalse(self.paths["state_file"].exists())

    def test_session_preserves_descriptors_order_and_restores_after_interrupt(self):
        candidate = self.make()
        try:
            candidate.setup()
            self.assertEqual(gadget.read_text(self.config / "rockchip/UDC"), "")
            for name in ("gamepad", "keyboard"):
                self.assertEqual((candidate.path / "functions" / f"hid.{name}/report_desc").read_bytes(),
                                 bytes.fromhex(self.profile[name]))
            links = [path.name for path in candidate.created if path.is_symlink()]
            self.assertEqual(links, ["hid.gamepad", "hid.keyboard"])
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            candidate.close()
        self.assert_vendor_preserved()

    def test_foreign_existing_adapter_name_is_not_touched(self):
        foreign = self.config / gadget.NAME
        foreign.mkdir()
        (foreign / "UDC").write_text("foreign-controller")
        candidate = self.make()
        with self.assertRaises(RuntimeError):
            candidate.setup()
        candidate.close()
        self.assertEqual((foreign / "UDC").read_text(), "foreign-controller")
        self.assertEqual(candidate.writes, [])

    def test_changed_controller_ownership_retains_recovery_receipt(self):
        candidate = self.make()
        candidate.setup()
        other = self.config / "other"
        other.mkdir()
        (other / "UDC").write_text("ff740000.usb")
        (self.udcs / "ff740000.usb/function").write_text("other")
        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            candidate.close()
        self.assertEqual(gadget.read_text(other / "UDC"), "ff740000.usb")
        self.assertTrue(self.paths["state_file"].exists())

    def test_live_cleanup_refuses_an_unexpected_gadget_controller(self):
        candidate = self.make()
        candidate.setup()
        (candidate.path / "UDC").write_text("another.usb")
        before = len(candidate.writes)
        with self.assertRaisesRegex(RuntimeError, "controller changed"):
            candidate.close()
        self.assertEqual(len(candidate.writes), before)
        self.assertTrue(self.paths["state_file"].exists())

    def test_recorded_crash_can_restore_vendor_without_recreating_it(self):
        candidate = self.make()
        candidate.setup()
        with patch.object(gadget, "Gadget", FakeGadget):
            gadget.recover(self.profile, **self.paths)
        self.assert_vendor_preserved()
        self.assertFalse(self.paths["state_file"].exists())

    def test_recovery_rejects_tampered_identity(self):
        candidate = self.make()
        candidate.setup()
        (candidate.path / "idProduct").write_text("0x1234")
        with self.assertRaisesRegex(RuntimeError, "identity differs"):
            gadget.recover(self.profile, **self.paths)
        self.assertEqual(gadget.read_text(candidate.path / "UDC"), "ff740000.usb")

    def test_recovery_rejects_path_traversal(self):
        self.paths["state_file"].write_text(json.dumps({"schema": 1, "vid": self.profile["vid"],
            "pid": self.profile["pid"], "udc": "../../outside", "original": "rockchip"}))
        with self.assertRaises(ValueError):
            gadget.recover(self.profile, **self.paths)
        self.assert_vendor_preserved()

    def test_probe_is_read_only(self):
        with patch.object(Path, "write_text", side_effect=AssertionError("unexpected mutation")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected mutation")), \
             patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mutation")):
            report = device.probe()
        self.assertIn("hid_gadget", report)


if __name__ == "__main__":
    unittest.main()
