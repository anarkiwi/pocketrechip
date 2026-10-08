# Upstream sources

Clones live in the gitignored `cache/src/` (refresh with `scripts/fetch-sources.sh`).

## Current Debian trixie port (NextThingCo `x-chip-*`, 2026)

| Repo | Pinned | Role |
|---|---|---|
| [x-chip-os](https://github.com/NextThingCo/x-chip-os) | `f9191c2` | live-build rootfs flavors `headless`, `gui`, `pocketchip` (X + libinput + awesome); releases ship `pocketchip-rootfs.tar.gz` |
| [x-chip-linux-deb](https://github.com/NextThingCo/x-chip-linux-deb) | `6ed9015` | Debian kernel source + armmp config + `nand.cfg` fragment, built as a `-chip` flavor |
| [x-chip-uboot](https://github.com/NextThingCo/x-chip-uboot) | `0e17d16` | mainline U-Boot for CHIP with DIP (1-Wire) detection |
| [x-chip-tools](https://github.com/NextThingCo/x-chip-tools) | `215f98e` | FEL flasher (`update.sh pocketchip`), Hynix/Toshiba detection, installer initramfs |
| [x-chip-deb-repo](https://github.com/NextThingCo/x-chip-deb-repo) | `20bb1e8` | apt repo (GitHub Pages) for the kernel and CHIP packages |
| [CHIP-dt-overlays](https://github.com/NextThingCo/CHIP-dt-overlays) | `e79aee3` | `firmware/early/x-chip-pocketchip.dts`: mainline PocketCHIP overlay |
| [chip-configs](https://github.com/NextThingCo/chip-configs) | `81f1606` | touchscreen calibration, XKB `pocketchip` layout, udev, kbd keymap |
| [pocketchip-configs](https://github.com/NextThingCo/pocketchip-configs) | `788c3fa` | session skel (awesome, lxterminal, GTK) |

## Hardware bring-up facts (mainline)

| Item | Value | Source |
|---|---|---|
| NAND layout | SPL, SPL.backup, U-Boot, env at 4 MiB each from 0; `rootfs` from `0x1000000` with `slc-mode` | `x-chip-linux-deb/sun5i-r8-chip.dts.nand.patch` |
| Root | `ubi.mtd=4 root=ubi0:rootfs rootfstype=ubifs fw_devlink=permissive` | `x-chip-os/*/bootscr.chip` |
| DIP detection | 1-Wire EEPROM, magic `CHIP`, PID 1 = PocketCHIP → `x-chip-pocketchip.dtbo` applied by U-Boot | `bootscr.chip` |
| Panel | `olimex,lcd-olinuxino-43-ts` (panel-simple), tcon0 RGB565, TVE disabled | overlay |
| Backlight | `pwm-backlight`, PWM0 8000 ns, enable PD18 | overlay |
| Keyboard | TCA8418 @ i2c1 0x34, IRQ PG1 falling, 6×10 matrix | overlay |
| Touchscreen | `sun4i-ts` (`rtp`, product `1c25000.rtp`), `allwinner,ts-attached` | overlay |
| Touch calibration | libinput `CalibrationMatrix "-1 0 1 0 -1.18 1.09 0 0 1"`; `sun4i-ts` ignores `touchscreen-inverted-*` | `chip-configs/X11/xorg.conf.d/99-calibration.conf` |
| Built-in drivers needed at boot | DRM, DRM_SUN4I, SUN4I_BACKEND, bridges, AXP20X power chain, MUSB dual-role | `x-chip-linux-deb/nand.cfg` |

## Known gaps

- `sun4i-ts` does not call `touchscreen_parse_properties()`, so orientation lives in an X-only matrix; console and Wayland clients get raw coordinates.
- The calibration matrix is one fixed value for all units; there is no per-device calibration step.
- Fastboot flashing of the SLC rootfs is broken (live installer path works).
- Long-term MLC reliability under SLC emulation is unverified.

## Legacy (reference only)

- [CHIP-linux](https://github.com/NextThingCo/CHIP-linux) branch `debian/4.4.13-ntc-mlc`: NTC 4.4 kernel with MLC/UBI patches (`bb/4.4/*`).
- `CHIP-dt-overlays/firmware/deprecated/dip-pocket-{common.dtsi,v72.dts,v73.dts}`: original NTC PocketCHIP overlays.
- [pocketChip-keyboardPatch](https://github.com/NextThingCo/pocketChip-keyboardPatch), [pocketchip-keypad-patch](https://github.com/NextThingCo/pocketchip-keypad-patch): 2016–17 keymap fixes.
