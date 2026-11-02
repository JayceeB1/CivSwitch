# LuckfoxSwitch — experimental Linux HID port

This adapter replaces the Pico's USB macro transport with Linux configfs HID.
It reuses the **unchanged** Pico generator, extracts its USB identity and report
descriptors, and replays the same keyboard/gamepad states and minimum holds.
It does not convert a Pico UF2 into a Luckfox firmware image.

**Status: Lyra Plus USB interfaces observed on a PC; Switch/game qualification
NOT_RUN.** Source parity and transactional USB cleanup are tested independently
of a console. Linux's
HID gadget driver assigns endpoint addresses and HID descriptors itself: this
is not a byte-identical reproduction of the Pico USB configuration descriptor.
For Linux 6.1, full-speed polling is normally 10 ms instead of the Pico's 1 ms;
the original minimum 32 ms keystroke holds are retained. Compatibility must be
checked on the actual Switch before calling this a working replacement.

## Requirements

- Luckfox Lyra Plus (or another Linux board with USB **device** mode).
- Python 3.9+ standard library; no compiler or pip packages on the board.
- Mounted USB configfs, libcomposite, and the **USB HID gadget** function
  `usb_f_hid` / `CONFIG_USB_CONFIGFS_F_HID`. `usbhid` is the host-side driver
  for reading keyboards and does **not** satisfy this requirement.
- A USB device controller and enough interrupt endpoints for two HID functions.
- English (US) keyboard/system setup as required by PicoSwitch.

The read-only `probe` command does not load modules, create functions, rebind a
controller, modify networking, or flash storage. Unknown kernel configuration
is reported as unknown; no `/dev/hidg*` by itself does not prove missing support.

## Prepare on the PC

Generate `PicoSwitch/payload.txt` with the existing `compress.js` workflow, then
run from the repository root (choose a new output directory):

```sh
python LuckfoxSwitch/prepare.py PicoSwitch/payload.txt /path/to/lyra-bundle
```

This runs the existing Pico macro generator and creates a standalone directory
containing `macro.bin`, the extracted `profile.json`, source/payload hashes,
and the Python runtime. The original Pico files are not modified. Invalid or
overlong input fails instead of silently dropping characters. Only preparation
uses the repository's source files; the resulting bundle is self-contained.

Transfer this directory through the board's existing SSH/SFTP/ADB connection.
On the board, inside that directory:

```sh
python3 device.py probe
python3 device.py check
```

`check` verifies the complete macro checksum, record count, report sizes,
bounded duration, and final release before any USB device is opened. A session
validates and retains an immutable snapshot (bounded to 8 MiB) before USB setup;
replacing the file while waiting cannot change the reports it will replay.
Each hold starts after the driver's pending USB write has completed, using the
Linux 6.1 `f_hid` POLLOUT/completion semantics rather than just write submission.

## USB session and restoration

### Installed automatic startup

`boot.py` and `S98civswitch` implement the Pico workflow: power the prepared
board from the deliberately selected Switch USB host and the macro starts
automatically once USB is configured, including the generated initial delay.
It runs once per service start, then keeps the HID gadget with neutral outputs.
There is no extra arm file or PC handoff requirement. Linux takes additional
time to boot; it is not a microcontroller with instantaneous USB startup.

The production service types immediately when attached to a host. It cannot
identify whether that host is a Switch or a PC. Revert its startup mode before
later reconnecting it to a PC for ordinary Murloc use.

On the qualified image, the runtime is installed in `/opt/civswitch`, its HID
module in the matching `/lib/modules/6.1.99/` tree, and the optional service in
`/etc/init.d/S98civswitch`. The source bundle alone does not install them.
`module.json` records the exact qualified module checksum and kernel release;
startup verifies both, loads the module, and waits for the stock USB service.
It holds that service's `/var/run/.usbdevice.lock` through setup and cleanup.

Select startup mode with root access over Ethernet/serial:

```sh
python3 /opt/civswitch/boot.py mode civswitch
# Later, when deliberately returning to Murloc:
python3 /opt/civswitch/boot.py mode murloc
```

An offline fallback is the SD card's `civswitch/mode.txt`: put `murloc` on its
own line, safely eject the card, then cold boot. This overrides the internal
`/etc/civswitch/mode`. `civswitch` selects automatic playback again. Invalid
mode contents select Murloc. No Murloc program or vendor USB configuration is
replaced. `boot.py stop` stops the current session and waits for USB restoration
without changing the selected startup mode.

For qualification on a PC **only**, the explicit bounded command below suppresses
all macro playback and gamepad-output echoes, then restores stock USB. It is a
diagnostic command, not the production startup behavior:

```sh
nohup python3 /opt/civswitch/boot.py boot --test-idle 20 >usb-test.log 2>&1 </dev/null &
```

### Temporary command-line sessions

The observed Lyra Plus firmware exposes `ff740000.usb` as its device controller
and uses it for the `rockchip` ADB/RNDIS gadget. The other port is configured as
a USB **host**. Use the actual names returned by `probe` on your own image.

An explicit replacement temporarily detaches the old gadget but leaves all its
functions, descriptors and links in place. BOTH new HID functions are created
before detaching it: missing HID support fails while the original network stays
bound. The original gadget is rebound after playback, interruption, timeout or
setup failure. Unknown ownership and concurrent adapter sessions fail closed.

**The USB network disconnects during a replacement.** Use Ethernet or serial
for supervision. For a board already cabled to the deliberately selected Switch
USB host, launch from Ethernet/serial:

```sh
python3 device.py run --udc ff740000.usb --replace-gadget rockchip --connection ready
```

For supervision initially over USB/RNDIS, the default `reconnect` mode waits for
one initial host enumeration, then a disconnect, then a new enumeration before
sending ANY macro. Run detached, and keep the board continuously powered while
moving the data cable from PC to Switch (suitable independent power required):

```sh
nohup python3 device.py run --udc ff740000.usb --replace-gadget rockchip >session.log 2>&1 </dev/null &
```

Do not select `ready` while connected to a PC: it sends the macro to that host.
A USB data connection cannot reliably identify the host as a Switch. In
`reconnect` mode, reconnecting to the PC also selects that PC as the new host.
Power loss ends a RAM-only session; moving an exclusively USB-powered board
without independent power cannot keep this process alive.

The default total session timeout is 1800 seconds (maximum 3600). Existing
Rockchip services must not concurrently reconfigure USB during a session. This
finite-session command does not stop or modify those services. SIGKILL and hardware failure
cannot run Python cleanup. After a process crash, while the board is still on:

```sh
python3 device.py recover
```

Recovery uses the adapter's root-owned `/run/civswitch-lyra.json` receipt and
checks gadget identity; it refuses foreign ownership. The receipt is retained
if restoration fails. Recovery never recreates or deletes the vendor gadget.

## Installed kernel prerequisite

The inspected Lyra Plus originally lacked `usb_f_hid`. A module was built from
the official April 2025 SDK using the board configuration and the same GCC
10.3.1 toolchain. The rebuilt/original `libcomposite.ko` `.text` sections match
exactly. The new HID module loaded on the original kernel, created a disposable
HID function, and exposed the gamepad and keyboard interfaces on Windows.
See [the build and qualification receipt](HID-MODULE.md). The existing kernel,
U-Boot and Murloc files are retained; no whole NAND image is flashed.

`kernel-hid.config` is the required configuration fragment. An unrelated module
bearing the same `6.1.99` release is not proof of ABI compatibility. This
repository contains source and a build recipe, not a universal flash image or
a precompiled module suitable for every Lyra firmware.

References: [Linux HID gadget](https://docs.kernel.org/usb/gadget_hid.html),
[Linux 6.1 HID driver](https://github.com/torvalds/linux/blob/v6.1/drivers/usb/gadget/function/f_hid.c),
[Luckfox kernel configuration](https://wiki.luckfox.com/Luckfox-Lyra/Kernel-Configuration/).

## Validation

```sh
python -m unittest discover -s LuckfoxSwitch/tests -v
```

Linux with `g++` also compiles and exercises the original `MacroEngine.cpp`
against the generated header, checking reports/timing against the Linux plan.
Windows runs the portable tests; native C++ parity is explicitly skipped there
when a compiler is unavailable. No console or configfs mutation is needed for
the test suite. Real USB descriptors, host requests, timing under load, sleep,
and successful in-game injection remain hardware acceptance checks.
