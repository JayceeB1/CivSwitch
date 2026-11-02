# Lyra Plus HID module provenance

This receipt applies to the inspected Lyra Plus, not every image with the same
release string. Original system: Linux 6.1.99, ARMv7 Thumb2, SMP/PREEMPT,
`mod_unload`, GCC 10.3.1 (Arm toolchain 10.3-2021.07, arm-10.29).

- Official [Luckfox SDK download index](https://wiki.luckfox.com/Luckfox-Lyra/Download/).
- Archive `Luckfox_Lyra_SDK_250429.tar.gz`, 3,200,025,554 bytes;
  SHA-256 `0498d819babf6c4254a7f1604ee52b95aff68ee154f87ecc34ce2883b330b4a8`.
- Kernel source commit `f91a6bea4294089633bb57f8710bc9694670570b`.
- Board config `luckfox_lyra_plus_buildroot_spinand_defconfig`; kernel config
  `rk3506_luckfox_defconfig` + `rk3506-display.config`; DTS
  `rk3506g-luckfox-lyra-plus`.
- Only added prerequisite: `CONFIG_USB_CONFIGFS_F_HID=y` (selects `USB_F_HID=m`).
  Module version CRCs are disabled in this configuration.

After the SDK's local repo checkout, use a separate output directory:

```sh
SDK=/path/to/Luckfox_Lyra_SDK
BUILD_OUT=/path/to/new-kernel-output
CROSS="$SDK/prebuilts/gcc/linux-x86/arm/gcc-arm-10.3-2021.07-x86_64-arm-none-linux-gnueabihf/bin/arm-none-linux-gnueabihf-"
make -C "$SDK/kernel-6.1" O="$BUILD_OUT" ARCH=arm CROSS_COMPILE="$CROSS" rk3506_luckfox_defconfig rk3506-display.config
"$SDK/kernel-6.1/scripts/config" --file "$BUILD_OUT/.config" --enable USB_CONFIGFS_F_HID
make -C "$SDK/kernel-6.1" O="$BUILD_OUT" ARCH=arm CROSS_COMPILE="$CROSS" olddefconfig
make -C "$SDK/kernel-6.1" O="$BUILD_OUT" ARCH=arm CROSS_COMPILE="$CROSS" -j8 vmlinux modules
"${CROSS}strip" --strip-debug "$BUILD_OUT/drivers/usb/gadget/function/usb_f_hid.ko"
```

The qualified stripped module is 17,248 bytes, SHA-256
`61590970ad309d8baf8dc92715ba03df1cb051b46c4d54c43d917223fc60b670`.
Its vermagic is `6.1.99 SMP preempt mod_unload ARMv7 thumb2 p2v8`.
The original and rebuilt `libcomposite.ko` `.text` sections are both 14,570 bytes,
SHA-256 `8cbaf80b0a76cc06d6c73b3f0c18f5f63945c61619fd4cb1cb27322514ac3391`.
This is ABI evidence in addition to the successful load and USB test; version
text alone is insufficient.

The module is the unmodified vendor Linux `drivers/usb/gadget/function/f_hid.c`,
licensed GPL-2.0. Preserve the vendor kernel source/license when rebuilding or
redistributing it. The standalone `prepare.py` bundle contains no module binary
until the board-specific qualified module and its `module.json` are added.

Preparation installed only new runtime/module/service files on the existing
root filesystem. Boot and U-Boot partitions were not replaced. Local NAND and
filesystem backups precede these writes, but the live rootfs dump is not an
atomic full-chip restore image and omits OOB and hidden pre-U-Boot areas.
