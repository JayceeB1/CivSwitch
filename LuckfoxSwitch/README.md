# LuckfoxSwitch — experimental Linux HID port

This adapter replaces the Pico's USB macro transport with Linux configfs HID.
It reuses the **unchanged** Pico generator, extracts its USB identity and report
descriptors, and replays the same keyboard/gamepad states and minimum holds.
It does not convert a Pico UF2 into a Luckfox firmware image.

**Status: software port; Switch hardware qualification NOT_RUN.** Source parity
and transactional USB cleanup are tested independently of a console. Linux's
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
adapter does not stop or modify those services. SIGKILL and hardware failure
cannot run Python cleanup. After a process crash, while the board is still on:

```sh
python3 device.py recover
```

Recovery uses the adapter's root-owned `/run/civswitch-lyra.json` receipt and
checks gadget identity; it refuses foreign ownership. The receipt is retained
if restoration fails. Recovery never recreates or deletes the vendor gadget.

## Installed kernel prerequisite

A read-only inspection on 2026-11-02 observed Linux 6.1.99/ARMv7, Python 3.11.8,
the bound `rockchip` gadget, and only FunctionFS/RNDIS gadget modules. No
`usb_f_hid.ko` was found in the installed module tree, and the readable kernel
symbol table contained no `hidg` symbols. The build configuration was unavailable.
These observations indicate that HID gadget support needs adding; a configfs
HID creation probe and Switch enumeration have **not** been performed.

`kernel-hid.config` is the required configuration fragment. Use the matching
Luckfox Lyra SDK, original board configuration, build flags and `Module.symvers`.
An unrelated module bearing the same `6.1.99` release is not proof of ABI
compatibility. The vendor workflow uses `./build.sh lunch`,
`./build.sh kernel-config`, then kernel/rootfs builds. Preserve the original
configuration and use a separately prepared development image before changing
an appliance. This repository supplies **no qualified replacement kernel,
flash operation, NAND migration, or automatic module installation**.

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
