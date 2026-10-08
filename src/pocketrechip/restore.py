"""Write a backup's nand.raw back, eraseblock by eraseblock, on the probe U-Boot.

Each eraseblock is erased through a one-eraseblock partition (bad blocks are skipped),
then an ECC read of its first page must succeed before `nand write.raw.noverify`, so a
block that is bad on the target is never written.
"""

import logging
from pathlib import Path

from . import steps as S
from .backup import (
    ECC_OK,
    MISMATCH,
    MTDIDS,
    PART,
    RAW_ADDR,
    RAW_FILE,
    RAW_OK,
    Backup,
    Geometry,
    eb_hashes,
    sha256_file,
)
from .fel_agent import STAT_ADDR, Agent
from .flash import check_backup, detect_chip
from .progress import progress_bar

log = logging.getLogger(__name__)


def chunk_step(geom: Geometry, raw: Path, e0: int, n: int, status: list) -> S.Step:
    """Erase and raw-write eraseblocks [e0, e0+n) whose backup raw read succeeded."""
    eb, reb = geom.eraseblock, geom.raw_eraseblock
    scratch = geom.ecc_addr(n)
    cmds = []
    for i in range(n):
        off = (e0 + i) * eb
        if not status[e0 + i] & RAW_OK:
            continue
        cmds += [
            f"setenv mtdparts nand0:{eb:#x}@{off:#x}({PART})",
            f"nand erase.part {PART}",
            f"if nand read {scratch:#x} {PART} {geom.page:#x}; then "
            f"if nand write.raw.noverify {RAW_ADDR + i * reb:#x} {off:#x} "
            f"{geom.pages:#x}; then mw.b {STAT_ADDR + i:#x} {S.OK}; fi; fi",
        ]
    return S.Step(
        f"eb{e0}-{e0 + n - 1}",
        tuple(cmds),
        tuple(f"eraseblock {e0 + i}" for i in range(n)),
        S.Data(raw, e0 * reb, n * reb),
        RAW_ADDR,
        strict=False,
    )


def plan(geom: Geometry, raw: Path, status: list, chunk: int) -> list[S.Step]:
    """mtdids, then one step per chunk of eraseblocks."""
    geom.check(chunk)
    return [
        S.chained("mtdids", [f"setenv mtdids {MTDIDS}"]),
        *(
            chunk_step(geom, raw, e0, min(chunk, geom.eraseblocks - e0), status)
            for e0 in range(0, geom.eraseblocks, chunk)
        ),
    ]


def _verify(agent: Agent, backup: Path, geom: Geometry, m: dict, chunk: int) -> list:
    """Eraseblocks whose ECC data differs from a stable backup read."""
    job = Backup(agent, backup, geom, chunk)
    bad = []
    with progress_bar(total=geom.eraseblocks, unit="eb", desc="verify") as progress:
        for e0 in range(0, geom.eraseblocks, chunk):
            n = min(chunk, geom.eraseblocks - e0)
            _, files = job.read_chunk(e0, n, raw=False)
            got = eb_hashes(files["ecc"], geom.eraseblock)
            files["ecc"].unlink()
            bad += [
                e0 + i
                for i, h in enumerate(got)
                if m["status"][e0 + i] & (ECC_OK | MISMATCH) == ECC_OK
                and h != m["ecc_sha256"][e0 + i]
            ]
            progress.update(n)
    return bad


def restore(
    agent: Agent, uboot: Path, backup: Path, chunk: int = 16, verify: bool = False
) -> dict:
    """Restore backup onto the detected chip; per-eraseblock outcome lists."""
    m = check_backup(backup)
    raw = Path(backup) / RAW_FILE
    if sha256_file(raw) != m["nand_raw_sha256"]:
        raise ValueError(f"{raw} does not match nand_raw_sha256 in the manifest")
    geom = Geometry(m["page"], m["pages"], m["oob"], m["eraseblocks"])
    steps = plan(geom, raw, m["status"], chunk)
    with agent.session(uboot):
        chip = detect_chip(agent)
        if chip.name != m["chip"]:
            raise ValueError(f"backup is of a {m['chip']}, board has {chip.name}")
        outcomes = S.run(agent, steps, "restore")[1:]
        status = b"".join(o.status for o in outcomes)
        want = [bool(s & RAW_OK) for s in m["status"]]
        failed = [e for e, s in enumerate(status) if want[e] and s != S.OK]
        result = {
            "written": sum(s == S.OK for s in status),
            "skipped": [e for e, w in enumerate(want) if not w],
            "failed": [e for e in failed if m["status"][e] & ECC_OK],
            "bad": [e for e in failed if not m["status"][e] & ECC_OK],
            "mismatch": _verify(agent, backup, geom, m, chunk) if verify else [],
        }
    log.info("restore: %s", result)
    return result
