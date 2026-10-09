# Flashing and restoring NAND

`pocketrechip flash` installs the Debian trixie image built by the
[anarkiwi/x-chip-os](https://github.com/anarkiwi/x-chip-os) fork (release `os-2026.10.09-071038`,
flavors `headless`, `gui`, `pocketchip`) with the
[anarkiwi/x-chip-uboot](https://github.com/anarkiwi/x-chip-uboot) release `uboot-2026.10.08-231804` over FEL, streaming the root filesystem through the
DFU agent loop of [probe.md](probe.md), so image size is bounded by the UBI volume, not DRAM.
`pocketrechip restore` writes a `pocketrechip backup` back. `pocketrechip install` runs
backup and flash for one board in one command.

## Usage

```sh
pocketrechip install [--flavor pocketchip] [--no-backup] [--verify-backup] [--wifi SSID]
pocketrechip restore [--backup DIR] [--verify]           # default: the board's install backup
pocketrechip backup --out cache/backup                   # the steps of install, by hand
pocketrechip flash --out DIR --backup cache/backup [--flavor pocketchip] [--dry-run]
tools/run.sh [--host HOST] flash --out /cache/flash --backup /cache/backup   # in Docker
```

Options: `--cache DIR` (downloads, images, per-board state; default `$POCKETRECHIP_CACHE`
or `cache`), `--chunk-mib N` (rootfs chunk, default 64), `--overlay DIR` / `--no-overlay`,
`--no-backup` (flash without a backup), `--chip toshiba|hynix` (dry-run plan),
`--uboot` (probe U-Boot), `--timeout` (seconds per DFU session), `--prepare-only` (fetch
and build the image only), `--wifi SSID`, `--wifi-open`, `--wifi-password-file FILE`.

## install

1. Image: fetch the release and build (or reuse) the UBIFS image.
2. Connect: wait for a board in FEL and read its SoC SID (`sunxi-fel sid`); its state is
   `cache/devices/<sid>/` (`backup/`, `flash/`), so boards never share a backup.
3. Backup: `pocketrechip backup` into `backup/`, resumed if incomplete, skipped if
   complete (`--verify-backup` re-reads each chunk). `--no-backup` skips it.
4. Flash: wait for FEL, then `pocketrechip flash` with that backup.

It prints a summary with the phase durations, the backup path, the login (`chip`/`chip`,
in `sudo`, from x-chip-os `0200-user.hook.chroot`) and what to do physically.
`pocketrechip restore` without `--backup` waits for a board the same way and restores
`cache/devices/<sid>/backup/`.

`install.sh` and `tools/run.sh` run these in the Docker image with the repo's `cache/` at
`/cache`, locally or on an ssh host (`--host`) that sees the repo at the same path. The
UBIFS build needs root: `install` and `flash` first run `--prepare-only` as root, then the
USB steps as the invoking user with the host's `plugdev` group, which the udev rule
`tools/70-pocketrechip.rules` gives access to `1f3a:efe8` (FEL) and `1f3a:1010` (agent
DFU). With `--wifi` the whole command runs as root (see below). Root-owned files in
`cache/` are handed back to the user afterwards.

## Wi-Fi profile

`--wifi SSID` (install, flash, qemu-smoke) adds a NetworkManager keyfile
`/etc/NetworkManager/system-connections/<SSID, [^A-Za-z0-9_-] as _>.nmconnection`,
`root:root` mode 0600 (NetworkManager ignores keyfiles readable by others), after the
overlay and before `mkfs.ubifs`:

- `[connection]` `id` (the SSID, GKeyFile-escaped), `uuid` (uuid5 of the SSID),
  `type=wifi`, `autoconnect=true`; `[wifi]` `mode=infrastructure`, `ssid`; `[ipv4]` and
  `[ipv6]` `method=auto`.
- `ssid=` is the plain string for printable ASCII without `;`, `\` or edge spaces, else
  the `b;b;...;` byte list (libnm-core `nm-keyfile.c` `ssid_writer`/`get_bytes`).
  `nmcli --offline connection modify` reads both forms back (`tests/test_wifi.py`).
- `[wifi-security]` `key-mgmt=wpa-psk`, `psk=` the 64-hex PSK
  `PBKDF2-HMAC-SHA1(password, SSID, 4096, 32)` as `wpa_passphrase` derives it; the
  password itself is not stored. `--wifi-open` omits the section.

The password comes from `--wifi-password-file FILE`, else `$POCKETRECHIP_WIFI_PASSWORD`,
else a no-echo prompt (asked twice); never from the command line. It must be 8-63
printable ASCII characters or 64 hex digits. The image holding the PSK is built in a
private 0700 temporary directory outside `cache/` and deleted after the flash, also on
failure; the overlay-only image stays cached. `--dry-run` prints only a redacted line.
`tools/run.sh` mounts a password file read-only and passes the variable through; because
the private image must be built and flashed by the same process, a `--wifi` run is one
root container.

The pocketchip flavour runs `openssh-server` (x-chip-os `headless.list.chroot`), so the
board is reachable as `ssh chip@<address>`; x-chip-os purges `avahi-daemon`, so the
address comes from the router or `ip a` on the device.

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
  (`tools/run.sh` runs `--prepare-only` as root first, then the device step as the user).
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
`overlay/remove`, then the update stamps below are written. Files and symlinks only: the
host has no armhf binfmt, so nothing runs in the image. `--no-overlay` builds the stock
rootfs.

The tar's `/etc/machine-id` is `uninitialized`, so the first boot runs systemd's
`preset-all` (enable-only): every unit with an `[Install]` section that no preset
disables is enabled, as Debian ships only `90-systemd.preset` and no `99-default.preset`.
A removed wants link alone is re-created then; masks and `disable` presets hold.

| Item | Reason |
|---|---|
| `etc/fstab`: `ubi0:rootfs / ubifs noatime,bulk_read 0 0` | The stock file is the `UNCONFIGURED` stub; `systemd-remount-fs` applies the options. Root is mounted rw by the kernel command line. `noatime` saves a NAND write per file read. `bulk_read` (Linux `Documentation/filesystems/ubifs.rst`) reads consecutive data nodes of a file in one go; otherwise each 4 KiB data node costs a whole 16 KiB NAND page read. On the device a cold read of a 32 MiB file took 23.5 s with `no_bulk_read` and 8.8 s with `bulk_read`, repeatably. |
| `etc/systemd/journald.conf.d/90-pocketrechip.conf`: `Storage=volatile` | `/var/log/journal` exists, so the journal would be persistent on NAND; keep it in RAM. There is no rsyslog. |
| `etc/systemd/system/zram-swap.service`, enabled by `swap.target.wants/zram-swap.service` | Compressed swap in RAM: `modprobe zram` (the 6.12 `-chip` kernel ships `zram.ko`), `zramctl --find --size` half of `MemTotal` (zram-generator's default size, `min(ram / 2, 4096)` MiB), `mkswap`, `swapon -p 100`, with the kernel's default compressor. Ordered after `systemd-modules-load`, before `swap.target`, without default dependencies; stop does `swapoff` and `zramctl --reset`. |
| `etc/sysctl.d/90-pocketrechip-zram.conf`: `vm.page-cluster = 0` | Swap readahead has no benefit on zram. |
| `plocate-updatedb.timer` masked (`-> /dev/null`) and its `timers.target.wants` link removed | Daily full-filesystem scans cost NAND reads, CPU and index writes. |
| `systemd-networkd.service`, `.socket`, `systemd-networkd-wait-online.service`, `systemd-network-generator.service` masked; their wants links and the `dbus-org.freedesktop.network1.service` alias removed | NetworkManager (`plugins=ifupdown,keyfile`) owns `wlan0`; networkd manages nothing (`/etc/systemd/network` is empty, `networkctl` shows every link unmanaged) and only `90-systemd.preset` enables it on first boot. Nothing else needs it: `apt-daily*.service` only order after it, `storage-target-mode.target` is not used and `network-online.target` is reached through `NetworkManager-wait-online.service`. |
| `graphical.target.wants/udisks2.service` removed; `etc/systemd/system-preset/80-pocketrechip.preset`: `disable udisks2.service` | Not started at boot; D-Bus activation (`org.freedesktop.UDisks2.service`, `SystemdService=udisks2.service`) starts it when a client such as gvfs asks. The preset keeps the first-boot `preset-all` from re-enabling it. Not masked, which would break activation. |
| `e2scrub_all.timer`, `e2scrub_reap.service`, `fstrim.timer` masked, their wants links removed | The root is UBIFS: no ext4, no LVM snapshots to scrub, no discard to trim. |
| `/etc/.updated`, `/var/.updated` (written by code, `update_done.py`) | See below. |

### Update stamps

Without `/etc/.updated` and `/var/.updated` every `ConditionNeedsUpdate=` unit runs on
the first boot (`ldconfig`, `systemd-journal-catalog-update`, `systemd-hwdb-update`,
`systemd-sysusers`, `systemd-update-done`), though the tar already holds their outputs.
`update_done.stamp` writes both stamps as `systemd-update-done` does in systemd v257 (the
tar's `libsystemd-shared-257.so`, Debian `257.13-1~deb13u1`):

- `src/update-done/update-done.c`: the three `# This file was created by
  systemd-update-done...` comment lines, then `TIMESTAMP_NSEC=` /usr's mtime in
  nanoseconds; the file's atime and mtime are set to /usr's mtime; mode 0644.
- `src/shared/condition.c` `condition_test_needs_update`: an update is needed when the
  stamp is missing or /usr's mtime is newer: seconds first, then nanoseconds, then
  `TIMESTAMP_NSEC=` when the stamp's mtime has no nanoseconds.

Only when no update is due: the units with `ConditionNeedsUpdate=` are read from the tree
(`/etc/systemd/system`, `/usr/local/lib/systemd/system`, `/usr/lib/systemd/system`,
drop-ins included, masked units skipped), and each must be in `update_done.OUTPUTS` with
every output a regular file at least as new as /usr:

| Unit | Output |
|---|---|
| `ldconfig.service` | `/etc/ld.so.cache` |
| `systemd-journal-catalog-update.service` | `/var/lib/systemd/catalog/database` |
| `systemd-hwdb-update.service` | `/usr/lib/udev/hwdb.bin`; `/etc/udev/hwdb.bin` when `/etc/udev/hwdb.d` has `*.hwdb` |
| `systemd-sysusers.service` | `/etc/passwd`, `/etc/group`, `/etc/shadow`, `/etc/gshadow` |
| `systemd-update-done.service` | the stamps themselves |

Otherwise (an unknown unit, a missing or older output) no stamp is written and the reasons
are logged. The overlay digest covers this step, so cached images are rebuilt.

### Boot time

`systemd-analyze` on the first device boot of the image without these items showed a
critical chain through `ldconfig.service` (`ConditionNeedsUpdate=/etc`) and
`sshd-keygen.service` to `ssh.service`, and, off the chain but costly on one core,
NetworkManager, udisks2, `dpkg-db-backup` (the timer's `Persistent=` catch-up),
systemd-networkd and bluetooth. Left as is:

- `sshd-keygen.service` generates the host keys on the first boot only; they must be
  unique per device, so they are not pre-generated in the image.
- NetworkManager, bluetooth and `dpkg-db-backup.timer`.

Reads from NAND bound much of the rest: about 5 MB/s raw from `/dev/ubi0_0`, and a cold
32 MiB file read took 23.5 s without `bulk_read` and 8.8 s with it.

`/tmp` is already tmpfs through Debian's `tmp.mount`. UBIFS compression stays zlib: raw
`/dev/ubi0_0` reads run at about 5 MB/s on the device, while zlib inflate on the A8 runs
at about 18 MB/s with a 2.3x ratio, so compressed reads are faster than uncompressed ones.

## QEMU smoke test

```sh
tools/run.sh qemu-smoke [--no-overlay] [--wifi SSID]
```

Options: `--flavor`, `--overlay DIR` / `--no-overlay`, `--wifi SSID` (with the Wi-Fi
options above), `--timeout` (seconds until QEMU is killed, default 3600). It needs root,
like the UBIFS build.

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
  options> 0 0` without the UBIFS-only options (`qemu_smoke.UBIFS_OPTIONS`, the `tokens`
  table of Linux `fs/ubifs/super.c`: `bulk_read`, `no_bulk_read`, `chk_data_crc`,
  `compr=`, ...), so `systemd-remount-fs` applies the overlay's `noatime` as on NAND (the
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
| `systemctl show` of the masked units | all `LoadState=masked`, `ActiveState=inactive` | otherwise |
| `udisks2.service` after boot, `busctl call org.freedesktop.UDisks2 /org/freedesktop/UDisks2/Manager org.freedesktop.DBus.Properties Get ss org.freedesktop.UDisks2.Manager Version`, then `systemctl is-active udisks2` | `inactive`, `v s "<version>"` (the last call), `active` | otherwise |
| `ldconfig`, `systemd-hwdb-update`, `systemd-journal-catalog-update` | `ConditionResult=no` with a condition timestamp (skipped this boot) | otherwise |
| `NetworkManager.service` | `active` | same |
| failing units: `ActiveState=failed`, `Result` not `success`, or `NRestarts` > 0 | only expected ones | only expected ones |
| `stat` of the `--wifi` keyfile | `600 root` | same |
| `nmcli -t -f NAME connection show` | lists the `--wifi` SSID | same |

The Wi-Fi rows apply with `--wifi`; QEMU has no radio (no `wlan0`), so the profile is
loaded, never connected. Under TCG udisks2 can take longer to start than dbus-daemon's
25 s `service_start_timeout`, so a failed first call (which still started the unit) is
followed by waiting for `udisks2.service` to be active and calling again.
`systemd-hwdb-update` is skipped without the overlay too (its
other conditions need `/etc/udev/hwdb.d` sources or a missing `/usr/lib/udev/hwdb.bin`).

Expected failures (`qemu_smoke.EXPECTED_FAILURES`): `ubihealthd.service` runs
`ubihealthd -d /dev/ubi0` with `Restart=on-failure`; QEMU has no NAND or UBI, so it
restarts in a loop, too slowly to hit its start limit, which is why `NRestarts` and not
`ActiveState=failed` catches it. Not a unit failure: under TCG, udev's coldplug can take
longer than the 90 s device timeout for `dev-ttyAMA0.device`, so
`serial-getty@ttyAMA0` (generated from `console=ttyAMA0`; the PocketCHIP console is
`ttyS0`) may log a dependency failure. The exit status is non-zero when the boot does
not finish or any check fails; the system state, `systemd-analyze` and the top of
`systemd-analyze blame` are printed for information.

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
| networkd, e2scrub, fstrim units | all `masked`, `inactive` | `loaded`; networkd, its socket, the generator and both timers `active` |
| `udisks2.service` | `inactive` after boot; the first call hits the 25 s activation timeout, the second returns `v s "2.10.1"`; then `active` | `active` after boot |
| `ldconfig`, `systemd-journal-catalog-update` | skipped (`ConditionResult=no`) | ran |
| `systemd-hwdb-update` | skipped | skipped |
| `NetworkManager.service` | `active` | `active` |
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
