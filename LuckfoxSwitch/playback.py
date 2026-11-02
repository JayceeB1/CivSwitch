"""Linux HID replay with bounded nonblocking writes and minimum hold durations."""
import os
import select
import time

from macro import GAMEPAD_NEUTRAL, KEYBOARD_NEUTRAL, iter_steps


class HidPair:
    def __init__(self, nodes, timeout=2, echo_gamepad=True):
        self.fds, self.timeout = {}, timeout
        self.echo_gamepad = echo_gamepad
        self.last = {}
        try:
            for name in ("gamepad", "keyboard"):
                self.fds[name] = os.open(nodes[name], os.O_RDWR | os.O_NONBLOCK)
        except BaseException:
            self.close()
            raise

    def write(self, name, data):
        fd = self.fds[name]
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [fd], [], max(0, left))[1]:
                raise TimeoutError(f"{name} endpoint is not being polled by its USB host")
            try:
                count = os.write(fd, data)
            except BlockingIOError:
                continue
            if count != len(data):
                raise OSError("Short HID report write")
            # Linux 6.1 f_hid POLLOUT is !write_pending; completion clears it.
            # A successful write only queues the transfer. Wait for completion
            # before the caller starts measuring the minimum hold duration.
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [fd], [], max(0, left))[1]:
                raise TimeoutError(f"{name} USB transfer did not complete")
            return

    def send(self, keyboard, gamepad):
        for name, data in (("gamepad", gamepad), ("keyboard", keyboard)):
            if self.last.get(name) != data:
                self.write(name, data)
                self.last[name] = data

    def hold(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            readable = select.select(list(self.fds.values()), [], [], left)[0]
            for fd in readable:
                try:
                    data = os.read(fd, 64)
                except BlockingIOError:
                    continue
                if not data:
                    raise OSError("HID host disconnected")
                # Pico echoes gamepad OUT reports; keyboard LED reports are just drained.
                if self.echo_gamepad and fd == self.fds["gamepad"] and len(data) == 8:
                    self.write("gamepad", data)
                    self.write("gamepad", self.last.get("gamepad", GAMEPAD_NEUTRAL))

    def close(self):
        errors = []
        for name, data in (("gamepad", GAMEPAD_NEUTRAL), ("keyboard", KEYBOARD_NEUTRAL)):
            if name in self.fds:
                try:
                    self.write(name, data)
                except (OSError, TimeoutError) as exc:
                    errors.append(str(exc))
                finally:
                    os.close(self.fds[name])
        self.fds.clear()
        return errors


def replay(path, transport, cancelled=None):
    """Never catch up after a delayed write: hold each state for its full duration."""
    for duration, keyboard, gamepad in iter_steps(path):
        if cancelled is not None and cancelled():
            raise InterruptedError("Playback stop requested")
        transport.send(keyboard, gamepad)
        remaining = duration / 1000
        if cancelled is None:
            transport.hold(remaining)
        else:
            # Keep stop/fallback responsive even during an unusually long WAIT.
            # Each slice still holds its full minimum; never catch up on delay.
            while remaining > 0:
                if cancelled():
                    raise InterruptedError("Playback stop requested")
                interval = min(remaining, 0.25)
                transport.hold(interval)
                remaining -= interval
