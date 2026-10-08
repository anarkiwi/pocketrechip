# pocketrechip

Current Debian (trixie, armhf) with X and touchscreen on the NextThing PocketCHIP.

## Usage

```sh
pip install -e '.[dev]'
pocketrechip probe-script            # read-only U-Boot NAND probe script
pocketrechip analyze dram.bin        # decode a probe dump (--json for JSON)
docker build -t pocketrechip .       # tools image: sunxi-tools, dfu-util, probe U-Boot in /opt/pocketrechip
```

- `scripts/fetch-sources.sh`: clone the upstream repos into `cache/src/`.
- `tools/fel-probe/`: build the probe U-Boot and run the probe over FEL.

## Docs

- [docs/probe.md](docs/probe.md): FEL NAND probe procedure, safety properties, DRAM window layout.
- [docs/sources.md](docs/sources.md): upstream repos, pinned revisions, hardware bring-up facts, known gaps.
