import sys
import tempfile
import unittest
from contextlib import contextmanager, ExitStack, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boot
import playback
from macro import GAMEPAD_NEUTRAL, KEYBOARD_NEUTRAL


class BootControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root-mode"
        self.card = Path(self.tmp.name) / "card-mode"

    def test_no_config_is_murloc(self):
        self.assertEqual(boot.configured_mode(self.root, self.card), "murloc")

    def test_card_can_select_fallback_over_root_civswitch(self):
        self.root.write_text("civswitch\n")
        self.card.write_text("\ufeffMURLOC\n", encoding="utf-8")
        self.assertEqual(boot.configured_mode(self.root, self.card), "murloc")

    def test_root_civswitch_selects_automatic_pico_behavior(self):
        self.root.write_text("civswitch\n")
        self.assertEqual(boot.configured_mode(self.root, self.card), "civswitch")

    def test_invalid_or_oversized_config_falls_back(self):
        for value in ("unknown", "x" * 129):
            self.card.write_text(value)
            self.assertEqual(boot.configured_mode(self.root, self.card), "murloc")

    def test_production_replays_automatically_but_diagnostic_stays_neutral(self):
        for test_idle in (0, 20):
            with self.subTest(test_idle=test_idle), ExitStack() as stack:
                stop = Path(self.tmp.name) / "stop"
                stop.unlink(missing_ok=True)
                stack.enter_context(patch.object(boot, "STOP", stop))
                stack.enter_context(patch.object(boot, "configured_mode", return_value="civswitch"))
                snapshot = stack.enter_context(patch.object(boot, "snapshot_macro", return_value=(b"validated snapshot", {})))
                replay = stack.enter_context(patch.object(boot, "replay"))
                if not test_idle:
                    replay.side_effect = lambda *_args, **_kwargs: stop.write_text("stop")
                stack.enter_context(patch.object(boot, "load_profile", return_value={}))
                stack.enter_context(patch.object(boot, "ensure_module"))
                stack.enter_context(patch.object(boot, "ready_vendor", side_effect=lambda: nullcontext("test.udc")))
                stack.enter_context(patch.object(boot, "lock_file", side_effect=lambda *a, **k: nullcontext()))
                stack.enter_context(patch.object(boot, "read_text", return_value="configured"))
                stack.enter_context(patch.object(boot, "status"))
                stack.enter_context(patch.object(boot.signal, "signal"))
                stack.enter_context(patch.object(boot.signal, "alarm", create=True))
                stack.enter_context(patch.object(boot.signal, "SIGALRM", 14, create=True))
                gadget_class = stack.enter_context(patch.object(boot, "Gadget"))
                pair_class = stack.enter_context(patch.object(boot, "HidPair"))
                pair_class.return_value.hold.side_effect = lambda _: stop.write_text("stop")
                boot.run_boot(test_idle)
                if test_idle:
                    snapshot.assert_not_called()
                    replay.assert_not_called()
                else:
                    snapshot.assert_called_once_with(boot.HERE / "macro.bin")
                    replay.assert_called_once()
                    self.assertEqual(replay.call_args.args[0], b"validated snapshot")
                pair_class.assert_called_once_with(gadget_class.return_value.nodes(), echo_gamepad=not test_idle)
                pair_class.return_value.send.assert_called_once_with(KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL)
                gadget_class.return_value.close.assert_called_once()

    def test_cleanup_restores_gadget_even_if_transport_close_is_interrupted(self):
        events = []
        @contextmanager
        def defer():
            events.append("signals_deferred")
            try:
                yield
            finally:
                events.append("signals_restored")
        transport = Mock()
        transport.close.side_effect = KeyboardInterrupt
        gadget = Mock()
        gadget.close.side_effect = lambda: events.append("gadget_restored")
        with patch.object(boot, "deferred_signals", defer):
            with self.assertRaises(KeyboardInterrupt):
                boot.cleanup(transport, gadget)
        self.assertEqual(events, ["signals_deferred", "gadget_restored", "signals_restored"])

    def test_delayed_vendor_releases_its_lock_between_readiness_attempts(self):
        events = []
        @contextmanager
        def vendor_lock(*_args, **_kwargs):
            events.append("acquire")
            try:
                yield
            finally:
                events.append("release")
        with patch.object(boot, "STOP", self.root), \
             patch.object(boot, "lock_file", vendor_lock), \
             patch.object(boot, "read_text", side_effect=[None, "test.udc"]), \
             patch.object(boot.time, "sleep"):
            with boot.ready_vendor() as udc:
                self.assertEqual(udc, "test.udc")
                self.assertEqual(events, ["acquire", "release", "acquire"])
        self.assertEqual(events[-1], "release")

    def test_stop_requests_cancellation_before_waiting_for_worker_cleanup(self):
        @contextmanager
        def worker_lock(path, **_kwargs):
            if path == boot.WORKER_LOCK:
                self.assertTrue(self.card.exists())
            yield
        with patch.object(boot, "STOP", self.card), \
             patch.object(boot, "lock_file", worker_lock), \
             patch.object(boot, "read_text", return_value=None):
            boot.stop_worker()
        self.assertEqual(self.card.read_text(), "stop\n")
        self.assertFalse(self.root.exists())

    def test_duplicate_start_cannot_erase_stop_of_active_worker(self):
        self.card.write_text("stop\n")
        @contextmanager
        def busy_worker(path, **_kwargs):
            if path == boot.WORKER_LOCK:
                raise TimeoutError("existing worker owns USB")
            yield
        with patch.object(boot, "STOP", self.card), \
             patch.object(boot, "lock_file", busy_worker), \
             patch.object(boot, "run_boot") as run:
            with self.assertRaises(TimeoutError):
                boot.startup()
            run.assert_not_called()
        self.assertEqual(self.card.read_text(), "stop\n")


class IdleTransportTests(unittest.TestCase):
    def test_idle_drains_nonneutral_host_output_without_echoing_input(self):
        pair = playback.HidPair.__new__(playback.HidPair)
        pair.fds, pair.echo_gamepad = {"gamepad": 10, "keyboard": 11}, False
        pair.last = {"gamepad": GAMEPAD_NEUTRAL}
        with patch.object(playback.time, "monotonic", side_effect=[0, 0, 1]), \
             patch.object(playback.select, "select", return_value=([10, 11], [], [])), \
             patch.object(playback.os, "read", return_value=b"\xff" * 8), \
             patch.object(pair, "write") as write:
            pair.hold(0.5)
        write.assert_not_called()

    def test_cancellation_interrupts_a_long_macro_hold(self):
        transport = Mock()
        requested = [False]
        transport.hold.side_effect = lambda _seconds: requested.__setitem__(0, True)
        with patch.object(playback, "iter_steps", return_value=iter([(60000, KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL)])):
            with self.assertRaises(InterruptedError):
                playback.replay(b"unused", transport, cancelled=lambda: requested[0])
        transport.send.assert_called_once_with(KEYBOARD_NEUTRAL, GAMEPAD_NEUTRAL)
        self.assertLessEqual(transport.hold.call_args.args[0], 0.25)


if __name__ == "__main__":
    unittest.main()
