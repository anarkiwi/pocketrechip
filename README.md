# pocketrechip

Reflash a NextThing PocketCHIP with current Debian (trixie, armhf) with X, touchscreen,
keyboard and Wi-Fi: NextThingCo's [x-chip-os](https://github.com/NextThingCo/x-chip-os)
image plus this repo's [tuning overlay](overlay/).

## Requirements

- A Linux computer with [Docker](https://docs.docker.com/engine/install/).
- A micro-USB data cable, a jumper wire (or paper clip) and a charged PocketCHIP.

## Install

1. Switch the PocketCHIP off: hold the power button for 8 seconds.
2. Jumper FEL to GND: on the header along the PocketCHIP's top edge, connect the
   right-most pad, `FEL`, to the pad beside it, `GROUND`
   ([PocketCHIP docs](https://github.com/NextThingCo/docs/blob/stable/PocketCHIP-docs/source/includes/_glance.md#gpio-access)).
   On a bare CHIP these are header U14 pin 7 (FEL) and pin 39 (GND)
   ([CHIP docs](https://github.com/NextThingCo/docs/blob/stable/CHIP-docs/source/includes/_advanced.md#prepare-chip-for-flashing)).
3. Connect the PocketCHIP's micro-USB port to the computer; press the power button if it
   stays off.
4. Run `./install.sh` in this repo. To have it join Wi-Fi on first boot, run
   `./install.sh --wifi "MyNetwork"` (it prompts for the password). The Wi-Fi is
   2.4 GHz only, WPA2-Personal or open; WPA3-only networks are not supported.
5. When it prints `Done`: remove the jumper, unplug USB, hold the power button for
   8 seconds, then press it. Log in as `chip`, password `chip` (sudo); change the
   password with `passwd`.

The first run backs up the whole NAND to `cache/devices/<SID>/backup/` before anything
is written. This takes a while; if interrupted, run `./install.sh` again and it resumes.
The flash then erases the whole NAND: the old system is gone, only the backup keeps it.
State is kept per board (SoC SID), so one checkout can reflash several PocketCHIPs.

Options: `--flavor pocketchip|gui|headless`, `--no-backup`, `--verify-backup`,
`--wifi SSID` with `--wifi-open` or `--wifi-password-file FILE`; `./install.sh --help`
lists them all. A missing prerequisite (Docker access, `plugdev` group, the udev rule
`tools/70-pocketrechip.rules`) is reported with the command that fixes it.

## Restore the original system

With the board in FEL as in steps 1-3, `./install.sh restore` writes its backup back.

## Remote USB host

`./install.sh --host HOST [restore] [OPTIONS]` runs on the ssh host `HOST`, which has the
PocketCHIP on its USB, Docker, and this repo at the same path (a shared filesystem).

## Diagnostics and development

`tools/run.sh [--host HOST] COMMAND [ARGS]` runs any command in the Docker image, with
`cache/` mounted at `/cache`:

```sh
tools/run.sh probe --out /cache/probe              # read-only NAND survey over FEL
tools/run.sh analyze /cache/probe/dram.bin         # decode a probe dump
tools/run.sh backup --out /cache/backup [--verify] # full NAND backup
tools/run.sh flash --out /cache/flash --backup /cache/backup [--dry-run]
tools/run.sh restore --backup /cache/backup [--verify]
tools/run.sh qemu-smoke [--no-overlay] [--wifi SSID]  # boot the image under QEMU
tools/run.sh probe-script                          # print the probe's U-Boot script
```

```sh
pip install -e '.[dev]'
black --check src tests && pylint src tests
shellcheck install.sh tools/*.sh scripts/*.sh
pytest -n auto --cov
```

- `tools/device-check.sh [user@]HOST`: read-only health report of a booted PocketCHIP over ssh (`SSHPASS` with sshpass for password login).
- `tools/apt-repo-check.sh [PKG[=MIN]...]`: verify the published apt repo's signature (`keys/pocketrechip-apt.asc`) and package versions.
- `tools/device-switch-repo.sh [user@]HOST`: move a booted PocketCHIP from the NextThingCo apt repo to ours, upgrade, reboot, report.
- `scripts/fetch-sources.sh`: clone the upstream repos into `cache/src/`.
- `scripts/install-nand-image-builder.sh DEST`: build sunxi-tools' `sunxi-nand-image-builder`.
- `overlay/`: rootfs tuning applied before `mkfs.ubifs`.

## Docs

- [docs/flash.md](docs/flash.md): install, flash and restore, Wi-Fi profile, safety
  properties, rootfs overlay, QEMU smoke test.
- [docs/probe.md](docs/probe.md): FEL U-Boot agent, safety properties, probe, backup,
  probe window layout.
- [docs/sources.md](docs/sources.md): upstream repos, pinned revisions, hardware bring-up
  facts, known gaps.
