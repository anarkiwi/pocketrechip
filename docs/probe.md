# FEL NAND probe

A read-only survey and full backup of a CHIP/PocketCHIP NAND, run from FEL without
writing flash.
It identifies the chip size, what occupies each eraseblock (legacy NTC SPL/U-Boot/env,
UBI with its `image_seq` and erase counters, erased or unreadable blocks) and recovers
any old U-Boot environment.

## U-Boot agent

The probe and the backup run on the same FEL-booted U-Boot "agent"
(`src/pocketrechip/fel_agent.py`). The Docker image builds that U-Boot
(`x-chip-uboot` `0e17d16` on v2022.01) into `/opt/pocketrechip/u-boot-sunxi-with-spl.bin`;
`docker run --rm pocketrechip cat /opt/pocketrechip/u-boot-sunxi-with-spl.bin > u-boot.bin`
extracts it for use outside the container (`--uboot`).

- `sunxi-fel -p uboot u-boot-sunxi-with-spl.bin write 0x43100000 agent.scr`;
  U-Boot's `bootcmd_fel` sources the agent script:

  ```
  setenv dfu_alt_info 'cmd ram 0x43200000 0x100000'
  while itest 1 == 1; do dfu 0 ram 0; source 0x43200000; mw.l 0x43200000 0 4; done
  mw.l 0x1c20c94 3
  mw.l 0x1c20c90 0x14af
  while itest 1 == 1; do mw.l 0x1c20c94 3; done
  ```

- The host downloads a `mkimage` script into DFU alt `cmd` (`dfu-util -a cmd -D x.scr`)
  and ends the session with a bare DFU_DETACH (`dfu-util -a cmd -e`). `dfu 0 ram 0`
  returns, the loop sources the script, then clears its image header so a session
  without a download sources nothing.
- Each script ends by setting `dfu_alt_info` to `cmd` plus the DRAM areas it filled,
  named with a per-run sequence number (`probe1`, `stat3`, `raw3`, ...). The host polls
  `dfu-util -l` until that name appears, so a stale enumeration of the previous session
  is never mistaken for the new one, then uploads each area with `dfu-util -U -Z size`.
- `dfu-util -R` is never used: in `run_usb_dnl_gadget` (common/dfu.c) a USB reset after
  a detach makes U-Boot reset the board instead of returning to the loop.
- The last script resets through the watchdog (`fel_agent.reset_commands`): `WDT_MODE`
  (`0x01c20c94`) = RESET_EN|EN with the 0.5 s interval, `WDT_CTRL` (`0x01c20c90`) =
  KEY|RESTART, then rewrite `WDT_MODE` forever. U-Boot's `reset` goes through sysreset
  (`sunxi_wdt_expire_now`, which arms the watchdog once) and bypasses the sun5i `reset_cpu()`
  loop that keeps rewriting `WDT_MODE` because sun5i sometimes gets stuck otherwise.
  With the FEL pin still grounded the board comes back in FEL (`1f3a:efe8`). DFU is `1f3a:1010`.

| Address | Use |
|---|---|
| `0x43100000` | agent boot script (FEL write) |
| `0x43200000` | `cmd` entity, 1 MiB |
| `0x43300000` | per-script status (`STAT_ADDR`) |
| `0x44000000`.. | probe window / backup data, below `0x58000000` |

## Safety properties

- `CONFIG_SYS_NAND_USE_FLASH_BBT` is disabled: bad blocks are judged from OOB markers
  only and no bad block table is ever scanned for or written to flash.
- `CONFIG_ENV_IS_NOWHERE=y`: the environment is never loaded from or saved to NAND.
- The agent loop never falls through to `bootcmd`, so it cannot reach `ubi part rootfs`
  (which would attach and possibly rewrite UBI metadata).
- Probe, detection and backup scripts use only `nand read`, `nand read.raw`, `mw`, `cp.l`,
  `itest` and `setenv`; no erase, write, `saveenv`, `ubi`, `dfu` or `reset`
  (asserted by `tests/test_probe_script.py` and `tests/test_backup.py`).
- The Docker build asserts the U-Boot config (no flash BBT, env nowhere, hush, `itest`,
  `mtdparts`, DFU RAM).
- Reads past the end of the chip fail cleanly; their failure is the size probe.

## Probe

1. Put the board in FEL (FEL pin grounded) on USB.
2. `pocketrechip probe --out DIR [--uboot PATH] [--json]` boots the agent, runs the probe
   script (`pocketrechip probe-script` prints it), uploads the window as `DIR/dram.bin`,
   resets the board and prints the analysis. `pocketrechip analyze DIR/dram.bin` re-decodes it.
   Exit status is non-zero if the script did not finish.
3. From a workstation: `tools/fel-probe/remote.sh HOST probe probe` builds the image on
   `HOST` from this repo and writes to the untracked `cache/probe/`. `HOST` must see the
   repo at the same path (shared filesystem); the container runs as the invoking user
   with the host's `plugdev` group.

## Backup

`pocketrechip backup --out DIR [--uboot PATH] [--chunk-ebs 16] [--oob N] [--eraseblocks N] [--verify]`

- A detection session (`probe_script.detect_commands`) first does a guarded `nand read`
  of page 0 (any outcome; the controller only holds the ID byte after a NAND command),
  copies the controller registers to `STAT_ADDR`, then does guarded one-page reads at
  4 GiB and 8 GiB, serving registers and the three status bytes as `detect<seq>`.
- The size probes decide the chip (`probe_layout.nand_size`, shared with the analyzer):
  4 GiB read fails means a 4 GiB part, 4 GiB ok and 8 GiB failed an 8 GiB part.
  A non-zero ID byte must agree (`probe_layout.identify`), otherwise the backup stops naming
  both. Geometry comes from `NAND_CHIPS` (keyed by size); `--oob`/`--eraseblocks` override,
  and are required when the size is inconclusive. Resuming requires the same chip name.
- Per chunk of eraseblocks, one script clears its DRAM areas and, per eraseblock *e*:
  - `nand read.raw RAW+i*raw_eb e*0x400000 0x100`: 256 pages, each data then OOB
    (`raw_eb = 256*(0x4000+oob)`), status bit 0;
  - `setenv mtdparts nand0:0x400000@<e*0x400000>(blk)` and
    `nand read ECC+i*0x400000 blk 0x400000`: ECC-corrected, de-randomised data, status bit 1.
    The one-eraseblock partition limits `nand_read_skip_bad`, so a bad block fails the
    read instead of silently returning the next good block's data.
- `RAW = 0x44000000`, `ECC` is the next 16 MiB boundary after the raw area; chunks whose
  areas would reach `0x58000000` are rejected (at most 37 Toshiba / 36 Hynix eraseblocks).
- The host appends the raw and ECC areas to `DIR/nand.raw` and `DIR/nand.ecc` and rewrites
  `DIR/manifest.json` after every chunk, logging one line per chunk (eraseblock range,
  status bit counts, MB/s, ETA); a rerun with the same `--out` resumes at the next
  eraseblock (the manifest must match chip geometry and U-Boot sha256). Failed slots are zero.
- `manifest.json`: chip, `nfc_id`, `page`, `pages`, `oob`, `eraseblocks`, `raw_eraseblock`,
  `chunk_eraseblocks`, `uboot_sha256`, per-eraseblock `status` and `ecc_sha256`,
  `nand_raw_sha256`, `nand_ecc_sha256`, `started`/`updated`/`finished`, `complete`.
- Status bits: 1 raw read ok, 2 ECC read ok (0 for bad or uncorrectable blocks),
  with `--verify` 4 ECC re-read identical, 8 ECC re-read differs. Raw MLC reads may
  legitimately differ between reads, so only ECC data is compared.

### Restore

`nand.raw` holds, per eraseblock, 256 × (`0x4000` + oob) bytes exactly as `nand read.raw`
returned them (randomised data and OOB including ECC bytes), so writing it back with
`nand write.raw` reproduces the page contents:

```
nand erase <e*0x400000> 0x400000
nand write.raw <addr> <e*0x400000> 0x100     # addr holds bytes [e*raw_eb, (e+1)*raw_eb) of nand.raw
```

Leave eraseblocks that the target's `nand bad` lists untouched; `nand erase` skips them.

## Probe DRAM window

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

The ID byte reads `0x00` until the controller has run a NAND command; the size probe is authoritative.

## Analysis

- Eraseblock first pages are classed `read-failed`, `erased` (all `0xff`), `ubi`
  (`UBI#` EC header; CRC, version, EC, `vid_hdr_offset`, `data_offset`, `image_seq` decoded)
  or `data`, and reported as contiguous runs.
- U-Boot version strings are extracted from `uboot`, `env` and `raw_page0`.
- A U-Boot environment at the start of `env` is matched by CRC32 over power-of-two sizes
  from `0x2000` to one eraseblock, in single and redundant (flag byte) layouts; variables are
  reported even when no CRC matches. The legacy NTC environment occupies a full eraseblock.
- Eraseblocks 0–1 hold the BROM-format SPL, which the ECC read path does not decode, so they are expected to read as failed.

## Progress

On a TTY, progress is tqdm bars on stderr. When stderr is not a TTY (`docker run -d`,
`docker logs`) the bars print one line every 30 s and on completion, and INFO log lines
(one per backup chunk) go to stderr as well.
