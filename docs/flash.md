# Flashing and restoring NAND

`pocketrechip flash` installs the NextThingCo Debian trixie image (`x-chip-os` release
`os-2026.09.23-010738`, flavors `headless`, `gui`, `pocketchip`) with the `x-chip-uboot`
release `uboot-2026.09.13-122745` over FEL, streaming the root filesystem through the
DFU agent loop of [probe.md](probe.md), so image size is bounded by the UBI volume, not DRAM.
`pocketrechip restore` writes a `pocketrechip backup` back.

## Usage

```sh
pocketrechip backup --out cache/backup                       # first, on the old system
pocketrechip flash --out DIR --backup cache/backup [--flavor pocketchip] [--dry-run]
pocketrechip restore --out DIR --backup cache/backup [--verify]
tools/fel-probe/remote.sh HOST flash flash --backup /cache/backup   # via an ssh host
```

Options: `--cache DIR` (downloads and images, default `$POCKETRECHIP_CACHE` or `cache`),
`--chunk-mib N` (rootfs chunk, default 64), `--overlay DIR` / `--no-overlay`,
`--no-backup` (flash without a backup), `--chip toshiba|hynix` (dry-run plan),
`--uboot` (probe U-Boot), `--timeout` (seconds per DFU session).

## What you do physically

1. Ground the FEL pin (jumper FEL to GND) and connect USB. The board enumerates as `1f3a:efe8`.
2. Run `backup` once, then `flash`. The board resets twice during `flash` and comes back
   in FEL each time because the jumper is still on.
3. When `flash` prints `flash complete`, remove the FEL jumper and power-cycle.

## Host side

- Release assets download into `cache/releases/<tag>/` and are checked against the sha256
  table pinned in `src/pocketrechip/release.py`; a mismatch aborts.
- The rootfs tar is extracted as root with numeric owners, permissions and device nodes,
  the rootfs overlay is applied, and `mkfs.ubifs -m 16384 -e 0x1f8000 -c 4096 -x zlib`
  builds the image (x-chip-tools `flash-ubi.sh`). LEB `0x1f8000` is half the 4 MiB MLC
  eraseblock (SLC mode) minus two pages for the UBI headers. The image and its
  `/boot/boot.scr` are cached in `cache/ubifs/` keyed by the tar sha256, the overlay digest
  and the mkfs parameters. Building needs root: run it in the Docker image as root
  (`remote.sh` does a root `--dry-run` first, then the device step as the user).
- The BROM SPL eraseblock is built for the detected chip only, as x-chip-tools
  `lib-nand.sh` does: `sunxi-nand-image-builder -c 64/1024 -p 16384 -o <oob> -u 1024
  -e 4194304 -b -s` of `sunxi-spl.bin`, then four copies at pages 0/64/128/192, each
  followed by random padding pages built by the same tool (OOB 1280 Toshiba, 1664 Hynix).
  Debian's `sunxi-tools` does not ship the builder; `scripts/install-nand-image-builder.sh`
  builds it from sunxi-tools at the revision Debian packages.
- `u-boot-dtb.bin` is zero-padded to one eraseblock.
- `--dry-run` does all of this, writes every device script to `DIR/plan-<chip>/` and prints
  the plan without touching USB.

## Device sequence

1. Probe U-Boot session: detection (size probes + ID byte, as `backup`), then reset.
   The backup manifest must be complete, its `nand.raw` the recorded size, and its chip
   the detected chip, unless `--no-backup`.
2. Release U-Boot session (`u-boot-sunxi-with-spl.bin` from the release), detection again
   (must agree), then one script per step. Each step clears its status bytes, runs its
   commands chained so a failure stops the rest, and serves the status as `stat<k>`; the
   host uploads and checks it before the next step and aborts (resetting the board) on failure.

| Step | Commands |
|---|---|
| erase | `nand erase.chip` |
| spl | `nand write.raw.noverify 0x4b000000 0x0 0x100`, same at `0x400000` |
| uboot | `nand write 0x4b000000 0x800000 0x400000` |
| ubi | `ubi part rootfs`, `ubi createvol rootfs` (all space) |
| rootfs*i*of*n* | `ubi write.part 0x4b000000 rootfs <size> [<total> on the first]` |
| verify | `ubifsmount ubi0:rootfs`, `ubifsload 0x4b000000 /boot/boot.scr`, `itest ${filesize} == <size>` |

- Data goes to DFU RAM entity `img<k>` at `0x4b000000`, served by the previous step's script.
  Staging must end below `0x59800000`: DRAM top `0x60000000` minus the 104 MiB U-Boot
  reserve that `flash-ubi.sh` derives from the relocated U-Boot's footprint.
- `ubi write.part` (U-Boot v2022.01 `cmd/ubi.c`): with a full size it calls
  `ubi_start_update(full)` and writes the first piece; without it,
  `ubi_more_update_data` appends. Data is gathered per LEB in the volume's update buffer,
  so pieces need no alignment, and the update completes (and the volume is checked) when
  `full` bytes have arrived. The first call only checks the piece against the volume, so
  the host refuses an image larger than the volume would be on an all-good chip:
  `(eraseblocks - 4 - 4 - limit) * 0x1f8000` with `limit` from `get_bad_peb_limit`
  (20 per 1024 over the whole chip in SLC PEBs).
- The verify step's `/boot/boot.scr` is uploaded and compared byte for byte with the image's.
- The last script is the agent's watchdog reset; the board returns to FEL.

Progress: a tqdm bar over the bytes downloaded and one log line per step (status bytes,
MB/s, ETA).

## Safety properties

- Nothing is written before detection, the backup check and the host-side plan
  (sizes, capacity, DRAM bounds) have passed: the release U-Boot, which creates its flash
  bad block table at boot, is loaded only after that.
- Writing uses the release U-Boot because it has `CONFIG_SYS_NAND_USE_FLASH_BBT=y`, as the
  installed bootloader does and as the kernel's `nand-on-flash-bbt` expects. The table
  lives in the last good eraseblocks; with a U-Boot without it (the probe U-Boot), UBI
  could be laid over those blocks and later be overwritten when the installed U-Boot or
  the kernel writes the table. The probe U-Boot is used only for reading.
- `ubi part rootfs` relies on the release U-Boot's default `mtdparts`, which marks `rootfs`
  `slc`; the flash never overrides `mtdparts`.
- Every step's status is checked before the next one; the board is reset on any error.

## Rootfs overlay

`overlay/rootfs/` is copied over the extracted root (files 0644/0755 as git stores them,
directories 0755, symlinks as is, all `root:root`), after deleting the paths in
`overlay/remove`. Files and symlinks only: the host has no armhf binfmt, so nothing runs
in the image. `--no-overlay` builds the stock rootfs.

| Item | Reason |
|---|---|
| `etc/fstab`: `ubi0:rootfs / ubifs noatime 0 0` | The stock file is the `UNCONFIGURED` stub; `systemd-remount-fs` applies `noatime`, saving a NAND write per file read. Root is mounted rw by the kernel command line. |
| `etc/systemd/journald.conf.d/90-pocketrechip.conf`: `Storage=volatile` | `/var/log/journal` exists, so the journal would be persistent on NAND; keep it in RAM. There is no rsyslog. |
| `etc/systemd/system/zram-swap.service`, enabled by `swap.target.wants/zram-swap.service` | Compressed swap in RAM: `modprobe zram` (the 6.12 `-chip` kernel ships `zram.ko`), `zramctl --find --size` half of `MemTotal` (zram-generator's default size, `min(ram / 2, 4096)` MiB), `mkswap`, `swapon -p 100`, with the kernel's default compressor. Ordered after `systemd-modules-load`, before `swap.target`, without default dependencies; stop does `swapoff` and `zramctl --reset`. |
| `etc/sysctl.d/90-pocketrechip-zram.conf`: `vm.page-cluster = 0` | Swap readahead has no benefit on zram. |
| `plocate-updatedb.timer` masked (`-> /dev/null`) and its `timers.target.wants` link removed | Daily full-filesystem scans cost NAND reads, CPU and index writes. |

`/tmp` is already tmpfs through Debian's `tmp.mount`. UBIFS compression stays zlib.

## QEMU smoke test

```sh
docker run --rm -v "$PWD/cache:/cache" pocketrechip pocketrechip qemu-smoke --cache /cache [--no-overlay]
```

Options: `--flavor`, `--overlay DIR` / `--no-overlay`, `--timeout` (seconds until QEMU
is killed, default 3600). It needs root, like the UBIFS build.

`qemu-smoke` builds the tree `flash` gives `mkfs.ubifs` (`images.rootfs_tree`: the
extracted tar with the overlay applied) and boots it with its own `/vmlinuz` and
`/initrd.img` under full-system `qemu-system-arm` (`virt`, Cortex-A15, one core,
512 MiB as on the PocketCHIP). No binfmt or host emulator is involved.

- Root: the initrd carries `virtio_mmio` and `ext4` but no driver for any disk QEMU can
  present on `virt` (`virtio_blk`, `sd_mod`, `nvme`, `mmci`, `ahci`, `usb-storage` are
  modules outside it), and nothing on the image is regenerated. The kernel builds in
  `VIRTIO_PCI` and `VIRTIO_FS`, and initramfs-tools' `local` script mounts a `root=` that
  is not a `/dev` path as given with `rootfstype`, so `virtiofsd` serves the tree
  (`vhost-user-fs-pci`, tag `rootfs`, memfd-backed shared guest RAM) with
  `root=rootfs rootfstype=virtiofs rw console=ttyAMA0 panic=-1`.
- `highmem=off`: the kernel has no `ARM_LPAE`, so every `virt` device must sit below 4 GiB.
- Changes to the test copy only: fstab's `/` entry becomes `rootfs / virtiofs <its
  options> 0 0`, so `systemd-remount-fs` applies the overlay's `noatime` as on NAND (the
  stock fstab has no `/` entry and stays as is); `pocketrechip-smoke.service`
  (`WantedBy=` and `After=multi-user.target`) waits for
  `systemctl is-system-running --wait`, sets the console log level to 1 so kernel
  messages cannot split result lines, prints every probe line prefixed
  `@pocketrechip-smoke <probe>` to `/dev/console`, and powers off. It does not write to
  `/dev/ttyAMA0` directly: `serial-getty@ttyAMA0` hangs up descriptors opened there.
  `-no-reboot` turns the power-off, or a panic, into QEMU exiting.
- The console streams to `cache/qemu-smoke/<key>/serial.log` (key over the tar sha256,
  the overlay digest, the unit, the script and the kernel command line) with a progress
  bar over console lines; `virtiofsd.log` and `result.json` sit beside it.

| Check | With overlay | `--no-overlay` |
|---|---|---|
| `swapon --show=NAME,TYPE,SIZE --bytes` | one `/dev/zram*` partition: half of `MemTotal` (`free -k`, same boot) in whole KiB, rounded up to a page by zram, less the header page `mkswap` reserves | no such swap |
| `findmnt -no OPTIONS /` | has `noatime` | lacks it |
| `*.journal*` files | none in `/var/log/journal`, some in `/run/log/journal` | otherwise |
| `systemctl is-enabled plocate-updatedb.timer` | `masked` | not `masked` |
| `systemctl is-active zram-swap.service` | `active` | not `active` |
| `sysctl -n vm.page-cluster` | `0` | not `0` |
| failing units: `ActiveState=failed`, `Result` not `success`, or `NRestarts` > 0 | only expected ones | only expected ones |

Expected failures (`qemu_smoke.EXPECTED_FAILURES`): `ubihealthd.service` runs
`ubihealthd -d /dev/ubi0` with `Restart=on-failure`; QEMU has no NAND or UBI, so it
restarts in a loop, too slowly to hit its start limit, which is why `NRestarts` and not
`ActiveState=failed` catches it. Not a unit failure: under TCG, udev's coldplug can take
longer than the 90 s device timeout for `dev-ttyAMA0.device`, so
`serial-getty@ttyAMA0` (generated from `console=ttyAMA0`; the PocketCHIP console is
`ttyS0`) may log a dependency failure. The exit status is non-zero when the boot does
not finish or any check fails; `systemd-analyze` and the system state are printed for
information.

CI's `qemu-smoke` job runs it with the overlay, caching the release tar under its pinned
sha256 and keeping `cache/qemu-smoke` as an artifact.

### Validated under QEMU

`pocketchip` rootfs of `os-2026.09.23-010738`, `MemTotal` 494788 KiB:

| Check | Overlay | `--no-overlay` |
|---|---|---|
| swap | `/dev/zram0` partition, 253329408 bytes | none |
| `/` options | `rw,noatime` | `rw,relatime` |
| journal | `/run/log/journal` only | `/var/log/journal` only |
| `plocate-updatedb.timer` | `masked` | `enabled` |
| `zram-swap.service` | `active` | `inactive` |
| `vm.page-cluster` | `0` | `3` |
| failing units | `ubihealthd.service` (expected) | `ubihealthd.service` (expected) |

## Restore

`pocketrechip restore --backup DIR [--chunk-ebs 16] [--verify]` runs on the probe U-Boot:

- The manifest must be complete, `nand.raw` must match `nand_raw_sha256`, and the detected
  chip must be the backup's.
- Per chunk of eraseblocks the raw images are downloaded to `0x44000000`, then per
  eraseblock *e* whose backup raw read succeeded:

  ```
  setenv mtdparts nand0:0x400000@<e*0x400000>(blk)
  nand erase.part blk
  if nand read <scratch> blk 0x4000; then
    if nand write.raw.noverify <addr> <e*0x400000> 0x100; then <status ok>; fi; fi
  ```

  `nand erase` skips bad blocks, and the one-eraseblock partition makes the read of a bad
  block fail (`nand_read_skip_bad` exceeds the partition), so a block bad on the target
  is never written; an erased page reads back clean. `nand erase.chip` followed by
  `write.raw` would write factory-bad blocks too. Raw writes are not verified by U-Boot
  because raw MLC reads may legitimately differ.
- The result lists `failed` (good in the backup, not written now), `bad` (not written, and
  the backup could not ECC-read them either), `skipped` (no raw data in the backup).
  `--verify` re-reads ECC data of every eraseblock and lists `mismatch`es against the
  backup's per-eraseblock sha256 (stable reads only). Exit status is non-zero on failures
  or mismatches.
