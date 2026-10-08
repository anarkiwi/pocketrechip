# FEL NAND probe

A read-only survey of a CHIP/PocketCHIP NAND, run from FEL without touching flash.
It identifies the chip size, what occupies each eraseblock (legacy NTC SPL/U-Boot/env,
UBI with its `image_seq` and erase counters, erased or unreadable blocks) and recovers
any old U-Boot environment.

## Procedure

1. `tools/fel-probe/build-uboot.sh` builds `cache/fel-probe/u-boot-sunxi-with-spl.bin`
   (or use `/opt/pocketrechip/u-boot-sunxi-with-spl.bin` from the Docker image).
2. Put the board in FEL (FEL pin grounded) on USB to a host with Docker.
3. `tools/fel-probe/probe.sh <ssh-host>`:
   - writes the probe script (`pocketrechip probe-script` produces identical text) and wraps it with `mkimage`;
   - `sunxi-fel -p uboot u-boot-sunxi-with-spl.bin write 0x43100000 probe.scr`;
     U-Boot's `bootcmd_fel` sources the script;
   - the script fills the DRAM window, then serves it as DFU alt `probe`;
     the host runs `dfu-util -a probe -U dram.bin`, saved as `cache/fel-probe/dram.bin`.
4. `pocketrechip analyze cache/fel-probe/dram.bin [--json]` decodes the window.
   Exit status is non-zero if the script did not finish.

## Safety properties

- U-Boot is `x-chip-uboot` (`0e17d16`) on v2022.01 with `CONFIG_SYS_NAND_USE_FLASH_BBT`
  disabled: bad blocks are judged from OOB markers only and no bad block table is ever
  scanned for or written to flash.
- `CONFIG_ENV_IS_NOWHERE=y`: the environment is never loaded from or saved to NAND.
- The script uses only `nand read`, `nand read.raw`, `mw`, `cp.l` into DRAM, `setenv`,
  `dfu ... ram` and `reset`; no erase, write, `saveenv` or `ubi` command
  (asserted by `tests/test_probe_script.py`).
- The script never returns to `bootcmd`: it ends in DFU sessions followed by `reset`,
  so it cannot fall through to `ubi part rootfs` (which would attach and possibly
  rewrite UBI metadata). With the FEL pin still grounded the board resets back into FEL.
- Reads past the end of the chip fail cleanly; their failure is the size probe.

## DRAM window

Base `0x44000000`, length `0x2400000`. Offsets are relative to the base;
the single source of truth is `src/pocketrechip/probe_layout.py`.

| Offset | Size | Content |
|---|---|---|
| `0x0000` | 6 | status per region below: 0 not run, 1 ok, 2 read failed |
| `0x0100` | 1024 | status per eraseblock first-page read |
| `0x0ffc` | 4 | `0x45464f44` (LE) once the script finished |
| `0x100000` | `0x100` | NAND controller registers `0x01c03000..`; byte `+0x35` is the NAND ID byte |
| `0x200000` | `0x400000` | region 0 `uboot`: NAND `0x800000` (eraseblock 2) |
| `0x600000` | `0x400000` | region 1 `env`: NAND `0xc00000` (eraseblock 3; legacy env, or U-Boot.backup in the new layout) |
| `0xa00000` | `0x800000` | region 2 `rootfs_head`: NAND `0x1000000` (first two rootfs eraseblocks) |
| `0x1200000` | `0x4000` | region 3 `probe_4g`: one page at NAND 4 GiB (ok means chip > 4 GiB) |
| `0x1240000` | `0x4000` | region 4 `probe_8g`: one page at NAND 8 GiB |
| `0x1300000` | `0x4680` | region 5 `raw_page0`: `nand read.raw` of page 0 (data + OOB) |
| `0x1400000` | 1024 × `0x4000` | ECC read of the first page of eraseblock *n* at `+n*0x4000` |

NAND geometry: page `0x4000`, eraseblock `0x400000`, OOB 1664 (Hynix) / 1280 (Toshiba).

| ID byte | Chip | Size |
|---|---|---|
| `0x40` | Toshiba TC58TEG5DCLTA00 | 4 GiB |
| `0x60` | Hynix H27UCG8T2ETR | 8 GiB |

The ID byte is only meaningful if the controller last issued READ ID; the size probe is authoritative.

## Analysis

- Eraseblock first pages are classed `read-failed`, `erased` (all `0xff`), `ubi`
  (`UBI#` EC header; CRC, version, EC, `vid_hdr_offset`, `data_offset`, `image_seq` decoded)
  or `data`, and reported as contiguous runs.
- U-Boot version strings are extracted from `uboot`, `env` and `raw_page0`.
- A U-Boot environment at the start of `env` is matched by CRC32 over power-of-two sizes
  from `0x2000` to one eraseblock, in single and redundant (flag byte) layouts; variables are
  reported even when no CRC matches. The legacy NTC environment occupies a full eraseblock.
- Eraseblocks 0–1 hold the BROM-format SPL, which the ECC read path does not decode, so they are expected to read as failed.
