"""Install the NextThingCo Debian release on NAND through the FEL agent.

The probe U-Boot detects the chip before anything is written. Writing runs on the release
U-Boot, whose flash bad block table matches the installed bootloader and the kernel's
`nand-on-flash-bbt`, so UBI never occupies the blocks that table lives in.
"""

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import images as I
from . import probe_layout as L
from . import release as R
from . import steps as S
from .backup import MANIFEST, RAW_FILE, load_manifest
from .fel_agent import Agent, Runner
from .probe_script import detect

log = logging.getLogger(__name__)

STAGE_ADDR = 0x4B000000
DRAM_TOP = 0x60000000
UBOOT_TOP_RESERVE = 104 << 20
STAGE_LIMIT = DRAM_TOP - UBOOT_TOP_RESERVE
SPL_OFFS = (0, L.ERASEBLOCK)
UBOOT_OFF = 0x800000
CHUNK = 64 << 20


@dataclass(frozen=True)
class Prepared:
    """Host-side inputs for a flash."""

    flavor: str
    uboot_fel: Path
    spl: Path
    uboot: Path
    ubifs: I.Ubifs


def prepare(
    cache: Path,
    flavor: str,
    out: Path,
    overlay: Path | None = None,
    runner: Runner | None = None,
) -> Prepared:
    """Fetch and verify the release, pad U-Boot and build (or reuse) the UBIFS image."""
    boot = _boot(cache, out)
    tar = R.ROOTFS[flavor]
    ubifs = I.cached_ubifs(tar.sha256, overlay, cache) or I.build_ubifs(
        R.fetch(tar, cache), tar.sha256, overlay, cache, runner
    )
    return Prepared(flavor, *boot, ubifs)


def _boot(cache: Path, out: Path) -> tuple[Path, Path, Path]:
    """Release U-Boot for FEL, SPL and U-Boot padded to an eraseblock in out."""
    fetched = {a.name: R.fetch(a, cache) for a in (R.SPL, R.UBOOT_DTB, R.UBOOT_FEL)}
    out.mkdir(parents=True, exist_ok=True)
    return (
        fetched[R.UBOOT_FEL.name],
        fetched[R.SPL.name],
        I.pad_uboot(fetched[R.UBOOT_DTB.name], out / "u-boot.pad"),
    )


@contextmanager
def prepared(
    cache: Path,
    flavor: str,
    out: Path,
    overlay: Path | None = None,
    layer: I.Layer | None = None,
) -> Iterator[Prepared]:
    """prepare(); with a (secret-bearing) layer the UBIFS is private, deleted on exit."""
    if layer is None:
        yield prepare(cache, flavor, out, overlay)
        return
    boot = _boot(cache, out)
    tar = R.fetch(R.ROOTFS[flavor], cache)
    with I.private_ubifs(tar, overlay, layer) as ubifs:
        yield Prepared(flavor, *boot, ubifs)


def chunks(total: int, chunk: int) -> list[tuple[int, int]]:
    """(offset, size) pieces of total, the last one partial."""
    return [(o, min(chunk, total - o)) for o in range(0, total, chunk)]


def plan(chip: L.Chip, spl: Path, prep: Prepared, chunk: int = CHUNK) -> list[S.Step]:
    """Device steps installing prep on chip; spl is the chip's SPL eraseblock image."""
    total, scr = prep.ubifs.size, prep.ubifs.boot_scr.stat().st_size
    room = I.capacity(chip.eraseblocks)
    if total > room:
        raise ValueError(
            f"UBIFS of {total} bytes exceeds the {chip.name} volume ({room})"
        )
    largest = max(chunk, spl.stat().st_size, prep.uboot.stat().st_size, scr)
    if chunk < 1 or STAGE_ADDR + largest > STAGE_LIMIT:
        raise ValueError(
            f"staging {largest:#x} bytes at {STAGE_ADDR:#x} passes {STAGE_LIMIT:#x}"
        )
    if spl.stat().st_size != I.PAGES * (L.PAGE + chip.oob):
        raise ValueError(f"{spl} is not a {chip.name} SPL eraseblock")
    a = f"{STAGE_ADDR:#x}"
    stage = {"addr": STAGE_ADDR}
    pieces = chunks(total, chunk)
    return [
        S.chained("erase", ["nand erase.chip"]),
        S.chained(
            "spl",
            [f"nand write.raw.noverify {a} {o:#x} {I.PAGES:#x}" for o in SPL_OFFS],
            data=S.Data.file(spl),
            **stage,
        ),
        S.chained(
            "uboot",
            [f"nand write {a} {UBOOT_OFF:#x} {L.ERASEBLOCK:#x}"],
            data=S.Data.file(prep.uboot),
            **stage,
        ),
        S.chained("ubi", ["ubi part rootfs", "ubi createvol rootfs"]),
        *(
            S.chained(
                f"rootfs{i + 1}of{len(pieces)}",
                [
                    f"ubi write.part {a} rootfs {n:#x}"
                    + (f" {total:#x}" if o == 0 else "")
                ],
                data=S.Data(prep.ubifs.image, o, n),
                **stage,
            )
            for i, (o, n) in enumerate(pieces)
        ),
        S.chained(
            "verify",
            [
                "ubifsmount ubi0:rootfs",
                f"ubifsload {a} /{I.BOOT_SCR}",
                f"itest ${{filesize}} == {scr:#x}",
            ],
            prelude=(f"mw.b {a} 0 {scr:#x}", "setenv filesize 0"),
            result=scr,
            **stage,
        ),
    ]


def describe(steps: Iterable[S.Step]) -> str:
    """One line per step: number, name, data bytes, checks."""
    return "".join(
        f"{k:4d} {s.name:<14} {s.data.size if s.data else '':>10}  "
        f"{'; '.join(s.checks)}\n"
        for k, s in enumerate(steps, 1)
    )


def check_backup(path: Path) -> dict:
    """Manifest of a complete backup in path, with nand.raw of the recorded size."""
    m = load_manifest(path)
    if not (m and m.get("complete")):
        raise FileNotFoundError(
            f"no complete backup in {path} ({MANIFEST}): run "
            f"`pocketrechip backup --out {path}` first, or pass --no-backup"
        )
    raw = Path(path) / RAW_FILE
    want = m["eraseblocks"] * m["raw_eraseblock"]
    if not raw.exists() or raw.stat().st_size != want:
        raise ValueError(f"{raw} is not the {want} bytes {MANIFEST} records")
    return m


def detect_chip(agent: Agent) -> L.Chip:
    """Supported chip on a booted agent, from the size probes and ID byte."""
    size, nfc_id = detect(agent)
    chip = L.identify(size, nfc_id)
    if chip is None:
        raise ValueError(f"unsupported NAND (size {size}, ID byte {nfc_id:#04x})")
    log.info("%s, NAND ID byte %#04x", chip.name, nfc_id)
    return chip


def flash(
    agent: Agent,
    probe_uboot: Path,
    prep: Prepared,
    backup: Path | None,
    chunk: int = CHUNK,
) -> L.Chip:
    """Detect (probe U-Boot), match the backup, then write and verify (release U-Boot)."""
    manifest = check_backup(backup) if backup else None
    with agent.session(probe_uboot):
        chip = detect_chip(agent)
    if manifest and manifest["chip"] != chip.name:
        raise ValueError(
            f"backup in {backup} is of a {manifest['chip']}, not {chip.name}"
        )
    spl = I.spl_image(prep.spl, chip.oob, agent.workdir / "spl.nand", agent.runner)
    steps = plan(chip, spl, prep, chunk)
    agent.wait_fel()
    with agent.session(prep.uboot_fel):
        again = detect_chip(agent)
        if again != chip:
            raise ValueError(f"release U-Boot detects {again.name}, probe {chip.name}")
        got = S.run(agent, steps, "flash")[-1].result
        if got != prep.ubifs.boot_scr.read_bytes():
            raise ValueError(
                f"/{I.BOOT_SCR} read back from NAND differs from the image"
            )
        log.info("/%s read back from NAND matches", I.BOOT_SCR)
    return chip


def dry_run(
    prep: Prepared, chips: Iterable[L.Chip], out: Path, chunk: int = CHUNK
) -> str:
    """Plans for chips, with SPL images built and scripts written under out/plan-<chip>."""
    out.mkdir(parents=True, exist_ok=True)
    size = prep.ubifs.size
    text = [
        f"U-Boot for writing: {prep.uboot_fel}\n"
        f"UBIFS: {prep.ubifs.image}: {size} bytes, {-(-size // I.LEB)} LEBs, "
        f"{len(chunks(size, chunk))} chunks of {chunk:#x}\n"
    ]
    for chip in chips:
        spl = I.spl_image(prep.spl, chip.oob, out / f"spl-{chip.key}.nand")
        steps = plan(chip, spl, prep, chunk)
        S.write_scripts(steps, out / f"plan-{chip.key}")
        text.append(
            f"\n{chip.name}: volume {I.capacity(chip.eraseblocks)} bytes, "
            f"scripts in {out / f'plan-{chip.key}'}\n{describe(steps)}"
        )
    return "".join(text)
