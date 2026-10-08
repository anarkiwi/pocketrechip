# pocketrechip

Current Debian (trixie, armhf) with X and touchscreen on the NextThing PocketCHIP.

## Usage

```sh
pip install -e '.[dev]'
docker build -t pocketrechip .       # tools image: sunxi-tools, dfu-util, mkimage, mtd-utils, probe U-Boot
pocketrechip probe --out DIR         # FEL: read-only NAND survey, decoded (DIR/dram.bin)
pocketrechip backup --out DIR        # FEL: full NAND backup (nand.raw, nand.ecc, manifest.json)
pocketrechip flash --out DIR --backup BACKUP [--flavor pocketchip] [--dry-run]
                                     # FEL: install the Debian trixie release
pocketrechip restore --out DIR --backup BACKUP [--verify]   # FEL: write a backup back
pocketrechip analyze DIR/dram.bin    # decode a probe dump (--json for JSON)
pocketrechip probe-script            # print the probe's U-Boot script
```

Device commands need the board in FEL on USB, `sunxi-fel`, `dfu-util` and `mkimage`;
run them in the image with
`docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb -v DIR:/out pocketrechip pocketrechip backup --out /out`.

- `scripts/fetch-sources.sh`: clone the upstream repos into `cache/src/`.
- `tools/fel-probe/remote.sh HOST NAME CMD`: run a device command on an ssh host sharing this repo's filesystem; `cache/` is `/cache`, output in `cache/NAME/`.
- `scripts/install-nand-image-builder.sh DEST`: build sunxi-tools' `sunxi-nand-image-builder`.
- `overlay/`: rootfs tuning applied before `mkfs.ubifs`.

## Docs

- [docs/probe.md](docs/probe.md): FEL U-Boot agent, safety properties, probe, backup and restore, probe window layout.
- [docs/flash.md](docs/flash.md): flash and restore procedure, safety properties, rootfs overlay.
- [docs/sources.md](docs/sources.md): upstream repos, pinned revisions, hardware bring-up facts, known gaps.
