# Evidence and porting boundary

Inspected Pico source baseline: `10b38e9f5abfd13a4b0bec8af24b026e44567da3`.
No UF2/ELF firmware was supplied in that checkout. `save_template.bin` has the
`CIV7` save-file magic and is not a microcontroller firmware image. The available
firmware source is the authoritative input to this port.

## Seen in the Pico source

- `PicoSwitch/include/SwitchConsts.h`, symbols
  `switch_usb_device_descriptor`, `switch_usb_configuration_descriptor`,
  `switch_usb_report_descriptor`, `keyboard_usb_report_descriptor`: USB
  VID/PID `0f0d:0092`, gamepad interface first, boot keyboard second, HID report
  descriptors of 86/63 bytes. Both application reports are 8 bytes.
- `SWITCH_USB_MASK_A`: bit 2. Neutral gamepad bytes are
  `00 00 08 80 80 80 80 00`; neutral keyboard bytes are eight zeroes.
- `PicoSwitch/src/switchController/SwitchUsb.cpp`, `SwitchUsb::init`: polls both
  controller reports and sends them through separate TinyUSB HID interfaces.
  `tud_hid_set_report_cb` echoes gamepad OUT reports and handles keyboard LEDs.
- `PicoSwitch/tools/payload_to_macro.py`, `main`: five initial A presses,
  then Esc/A, US keyboard input, Enter twice, and separators between batches.
  Key holds are 32 ms, A holds 50 ms, Esc/A wait 750 ms, post-A wait 550 ms.
  Consecutive identical keycodes get a release; modifier changes alone do not.
- `PicoSwitch/include/MacroEngine.h`, `MACRO_START_DELAY_MS`: source default
  is **1000 ms** (the older Pico README says approximately ten seconds).
  `MacroEngine::tick` and `loadStep` define the actual state/hold semantics.

## Implemented and deterministically checked

Host preparation invokes that generator unchanged and decodes only its small
macro DSL. USB identity and report descriptors are extracted from its header,
not independently maintained. Printable input and bounded sizes are checked
before generation; a `};` inside a payload comment cannot end the parser early.

Native C++ tests compile the original `MacroEngine.cpp` with hardware stubs and
the original headers, then compare every emitted report/hold with the Linux
plan. Portable tests independently check all printable US ASCII and repeated
keys, immutable macro snapshots, USB completion barriers, failure-before-unbind,
interruption immediately after unbind, repeated cleanup failures, partial
creation recovery, and changed/foreign ownership.

Linux replay uses separate HID character devices identified by configfs
major/minor values. A finite session exclusively locks its own adapter, records
recovery intent before changing bindings, creates both HID functions before
detaching the original gadget, and retains the original gadget in configfs.
Cleanup reconciles the real binding and restores it. It never edits the vendor
gadget's functions or links, networking files, boot services, or storage image.

## Seen on one Lyra Plus, 2026-11-02 (read-only)

- Linux `6.1.99`, `armv7l`; Python `3.11.8`; configfs mounted.
- Device controller `ff740000.usb`, configured by the `rockchip` gadget using
  `ffs.adb` and `rndis.0`.
- Device tree: `usb@ff740000` is peripheral; `usb@ff780000` is host.
- The installed gadget module directory contains libcomposite, udc-core,
  u_ether, FunctionFS and RNDIS modules, with no `usb_f_hid.ko` found.
- The readable kernel symbol table contains no `hidg` symbols. The original
  kernel configuration is unavailable. The supplied standalone `probe.py` ran
  successfully on the board without writing files or changing USB bindings.

**Inferred:** this image needs the HID gadget function added before using this
transport. Host-side `usbhid` is not evidence of gadget support. A disposable
HID-function creation probe was not performed, so the exact Kconfig
setting and registration outcome remain unknown.

## Unknown / hardware qualification NOT_RUN

- Recognition of this Linux composite gadget by a Switch and actual in-game
  injection. No console was connected or modified by this task.
- Exact source/configuration/ABI needed for a replacement module for the
  installed vendor kernel. `kernel-hid.config` states the requirement; it is
  not a compiled or qualified driver or flash image.
- Linux USB descriptors are not identical to TinyUSB's: Linux 6.1 assigns
  endpoints, uses HID version 1.01, and full-speed polling of 10 ms, whereas
  the Pico declares HID 1.11 and 1 ms polling. Linux uses 8-byte max packets
  for these 8-byte reports; the Pico gamepad endpoint declares 64 bytes.
  Preserving VID/PID, interface order and report bytes does not prove that
  these descriptor differences are accepted by the console.
- Suspend/resume, physical cable/power arrangement, timing under board load,
  concurrent vendor USB reconfiguration, and successful post-session network
  restoration on hardware. Simulated rollback is not a physical-board test.

SIGKILL, power loss and other actors replacing USB state cannot be made
transactional across kernel configfs and userspace. Recovery fails closed on
unexpected ownership; a board reboot follows its existing firmware startup.
