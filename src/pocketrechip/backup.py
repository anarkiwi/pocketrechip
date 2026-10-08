"""Full NAND backup over the FEL agent: raw pages with OOB, and ECC-corrected data.

ECC reads go through a one-eraseblock mtd partition so a bad block fails the read
(`nand_read_skip_bad` exceeds the partition limit) instead of reading the next block.
"""

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from tqdm import tqdm

from . import probe_layout as L
from .fel_agent import STAT_ADDR, Agent, serve
from .probe_script import detect
from .progress import progress_bar

log = logging.getLogger(__name__)

RAW_ADDR = 0x44000000
DRAM_LIMIT = 0x58000000
ECC_ALIGN = 0x1000000
PART = "blk"
MTDIDS = "nand0=nand0"

RAW_OK, ECC_OK, VERIFIED, MISMATCH = 1, 2, 4, 8
STATUS_BITS = {"raw": RAW_OK, "ecc": ECC_OK, "verified": VERIFIED, "mismatch": MISMATCH}

MANIFEST, RAW_FILE, ECC_FILE, WORK_DIR = (
    "manifest.json",
    "nand.raw",
    "nand.ecc",
    ".work",
)
FORMAT = 1
HASH_BLOCK = 1 << 24


@dataclass(frozen=True)
class Geometry:
    """NAND geometry as seen by U-Boot."""

    page: int
    pages: int
    oob: int
    eraseblocks: int

    @property
    def eraseblock(self) -> int:
        """ECC data bytes per eraseblock."""
        return self.page * self.pages

    @property
    def raw_eraseblock(self) -> int:
        """Raw bytes per eraseblock (each page followed by its OOB)."""
        return self.pages * (self.page + self.oob)

    def ecc_addr(self, n: int) -> int:
        """ECC area base for a chunk of n eraseblocks."""
        end = RAW_ADDR + n * self.raw_eraseblock
        return -(-end // ECC_ALIGN) * ECC_ALIGN

    def check(self, n: int) -> None:
        """Reject chunks whose DRAM areas would reach DRAM_LIMIT."""
        end = self.ecc_addr(n) + n * self.eraseblock
        if n < 1 or end > DRAM_LIMIT:
            raise ValueError(f"chunk of {n} eraseblocks ends at {end:#x}")


def geometry(
    size: int | None,
    nfc_id: int,
    oob: int | None = None,
    eraseblocks: int | None = None,
) -> tuple[str, Geometry]:
    """Chip name and geometry from the size probes and ID byte, with overrides."""
    chip = L.identify(size, nfc_id)
    oob = oob or (chip and chip.oob)
    eraseblocks = eraseblocks or (chip and chip.eraseblocks)
    if not (oob and eraseblocks):
        raise ValueError(
            f"unknown NAND (size {size}, ID byte {nfc_id:#04x}): give oob and eraseblocks"
        )
    name = chip.name if chip else "unknown"
    return name, Geometry(L.PAGE, L.ERASEBLOCK // L.PAGE, oob, eraseblocks)


def chunk_commands(
    geom: Geometry, e0: int, n: int, seq: int, raw: bool = True
) -> list[str]:
    """Read eraseblocks [e0, e0+n) into DRAM; status byte per block in STAT_ADDR."""
    geom.check(n)
    eb, reb, ecc = geom.eraseblock, geom.raw_eraseblock, geom.ecc_addr(n)
    out = [f"mw.b {STAT_ADDR:#x} 0 {n:#x}", f"setenv mtdids {MTDIDS}"]
    if raw:
        out.append(f"mw.l {RAW_ADDR:#x} 0 {n * reb // 4:#x}")
    out.append(f"mw.l {ecc:#x} 0 {n * eb // 4:#x}")
    for i in range(n):
        st, off = f"{STAT_ADDR + i:#x}", (e0 + i) * eb
        if raw:
            out.append(
                f"if nand read.raw {RAW_ADDR + i * reb:#x} {off:#x} {geom.pages:#x}; "
                f"then mw.b {st} {RAW_OK}; fi"
            )
        out += [
            f"setenv mtdparts nand0:{eb:#x}@{off:#x}({PART})",
            f"if nand read {ecc + i * eb:#x} {PART} {eb:#x}; then "
            f"if itest.b *{st} == {RAW_OK}; then mw.b {st} {RAW_OK | ECC_OK}; "
            f"else mw.b {st} {ECC_OK}; fi; fi",
        ]
    ents = [(f"stat{seq}", STAT_ADDR, n)]
    if raw:
        ents.append((f"raw{seq}", RAW_ADDR, n * reb))
    ents.append((f"ecc{seq}", ecc, n * eb))
    return out + [serve(ents)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, desc: str | None = None) -> str:
    """sha256 of a file with byte progress."""
    h = hashlib.sha256()
    with (
        open(path, "rb") as f,
        progress_bar(
            total=path.stat().st_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc or path.name,
            leave=False,
        ) as progress,
    ):
        while block := f.read(HASH_BLOCK):
            h.update(block)
            progress.update(len(block))
    return h.hexdigest()


def eb_hashes(path: Path, size: int) -> list[str]:
    """sha256 of each size-byte slice of a file."""
    data = memoryview(path.read_bytes())
    return [
        hashlib.sha256(data[o : o + size]).hexdigest()
        for o in range(0, len(data), size)
    ]


def write_manifest(out: Path, manifest: dict) -> None:
    """Atomically replace out/manifest.json."""
    manifest["updated"] = _now()
    tmp = out / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1) + "\n")
    os.replace(tmp, out / MANIFEST)


def load_manifest(out: Path) -> dict | None:
    """Existing manifest, if any."""
    path = out / MANIFEST
    return json.loads(path.read_text()) if path.exists() else None


class Backup:
    """Chunked backup of all eraseblocks into out/nand.raw and out/nand.ecc."""

    def __init__(self, agent: Agent, out: Path, geom: Geometry, chunk: int):
        geom.check(chunk)
        self.agent, self.out, self.geom, self.chunk = agent, Path(out), geom, chunk

    def start(
        self, chip: str, nfc_id: int, uboot_sha256: str, prior: dict | None
    ) -> dict:
        """New manifest, or prior one if it describes the same device and U-Boot."""
        g = self.geom
        manifest = {
            "format": FORMAT,
            "chip": chip,
            "nfc_id": nfc_id,
            **asdict(g),
            "eraseblock": g.eraseblock,
            "raw_eraseblock": g.raw_eraseblock,
            "chunk_eraseblocks": self.chunk,
            "uboot_sha256": uboot_sha256,
            "status_bits": STATUS_BITS,
            "next_eraseblock": 0,
            "status": [],
            "ecc_sha256": [],
            "complete": False,
            "started": _now(),
            "finished": None,
        }
        if prior is None:
            if any((self.out / f).exists() for f in (RAW_FILE, ECC_FILE)):
                raise FileExistsError(f"{self.out} has NAND images but no {MANIFEST}")
            return manifest
        keys = ("format", "chip", *asdict(g), "uboot_sha256")
        diff = [k for k in keys if prior.get(k) != manifest[k]]
        if diff:
            raise ValueError(
                f"{self.out / MANIFEST} differs in {diff}; use a new --out"
            )
        prior["chunk_eraseblocks"] = self.chunk
        return prior

    def read_chunk(self, e0: int, n: int, raw: bool) -> tuple[np.ndarray, dict]:
        """Run one chunk script and upload its areas into the work dir."""
        g, agent = self.geom, self.agent
        seq = agent.next_seq()
        agent.run(f"chunk{seq}", chunk_commands(g, e0, n, seq, raw), f"stat{seq}")
        work = agent.workdir
        path = agent.dfu.upload(f"stat{seq}", work / f"stat{seq}.bin", n)
        stat = np.frombuffer(path.read_bytes(), np.uint8)
        path.unlink()
        sizes = [("raw", n * g.raw_eraseblock)] if raw else []
        files = {
            k: agent.dfu.upload(f"{k}{seq}", work / f"{k}{seq}.bin", s)
            for k, s in sizes + [("ecc", n * g.eraseblock)]
        }
        return stat, files

    def run(self, manifest: dict, verify: bool = False) -> dict:
        """Back up the remaining eraseblocks, updating manifest after each chunk."""
        g, out = self.geom, self.out
        start = manifest["next_eraseblock"]
        per_eb = g.raw_eraseblock + g.eraseblock
        paths = {"raw": out / RAW_FILE, "ecc": out / ECC_FILE}
        for k, size in (("raw", g.raw_eraseblock), ("ecc", g.eraseblock)):
            with open(paths[k], "ab") as f:
                if f.tell() < start * size:
                    raise ValueError(f"{paths[k]} shorter than {MANIFEST} records")
                f.truncate(start * size)
        status = manifest["status"]
        hashes = manifest["ecc_sha256"]
        t0 = time.monotonic()
        with progress_bar(
            total=g.eraseblocks * per_eb,
            initial=start * per_eb,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="backup",
        ) as progress:
            for e0 in range(start, g.eraseblocks, self.chunk):
                n = min(self.chunk, g.eraseblocks - e0)
                stat, files = self.read_chunk(e0, n, raw=True)
                ecc_h = eb_hashes(files["ecc"], g.eraseblock)
                if verify:
                    stat = stat | self.verify(e0, n, stat, ecc_h)
                for k, path in files.items():
                    with open(path, "rb") as src, open(paths[k], "ab") as dst:
                        shutil.copyfileobj(src, dst, HASH_BLOCK)
                        dst.flush()
                        os.fsync(dst.fileno())
                    path.unlink()
                status += stat.tolist()
                hashes += ecc_h
                manifest["next_eraseblock"] = e0 + n
                write_manifest(out, manifest)
                progress.update(n * per_eb)
                done = (e0 + n - start) * per_eb
                rate = done / max(time.monotonic() - t0, 1e-9)
                eta = (g.eraseblocks - e0 - n) * per_eb / rate
                log.info(
                    "eraseblocks %d-%d/%d: %s; %.2f MB/s, ETA %s",
                    e0,
                    e0 + n - 1,
                    g.eraseblocks,
                    _counts(stat),
                    rate / 1e6,
                    tqdm.format_interval(eta),
                )
        manifest["nand_raw_sha256"] = sha256_file(paths["raw"])
        manifest["nand_ecc_sha256"] = sha256_file(paths["ecc"])
        manifest["complete"] = True
        manifest["finished"] = _now()
        write_manifest(out, manifest)
        return manifest

    def verify(self, e0: int, n: int, stat: np.ndarray, ecc_h: list[str]) -> np.ndarray:
        """Re-read ECC data; VERIFIED/MISMATCH bits where both reads succeeded."""
        stat2, files = self.read_chunk(e0, n, raw=False)
        again = eb_hashes(files["ecc"], self.geom.eraseblock)
        files["ecc"].unlink()
        both = ((stat & stat2 & ECC_OK) != 0).astype(np.uint8)
        same = np.array([a == b for a, b in zip(ecc_h, again)], np.uint8)
        return both * np.where(same, VERIFIED, MISMATCH).astype(np.uint8)


@dataclass(frozen=True)
class Options:
    """Backup options; oob and eraseblocks default from the detected chip."""

    chunk: int = 16
    oob: int | None = None
    eraseblocks: int | None = None
    verify: bool = False


def backup(agent: Agent, out: Path, uboot: Path, opts: Options = Options()) -> dict:
    """Boot the agent, back up (or resume backing up) the whole NAND, reset."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    prior = load_manifest(out)
    if prior and prior["complete"]:
        return prior
    with open(uboot, "rb") as f:
        uboot_sha = hashlib.file_digest(f, "sha256").hexdigest()
    with agent.session(uboot):
        size, nfc_id = detect(agent)
        chip, geom = geometry(size, nfc_id, opts.oob, opts.eraseblocks)
        log.info("%s, NAND ID byte %#04x: %s", chip, nfc_id, geom)
        job = Backup(agent, out, geom, opts.chunk)
        return job.run(job.start(chip, nfc_id, uboot_sha, prior), opts.verify)


def _counts(status) -> str:
    """Eraseblocks with each status bit set."""
    st = np.asarray(status, np.uint8)
    return " ".join(f"{k}={int(((st & b) != 0).sum())}" for k, b in STATUS_BITS.items())


def summary(manifest: dict) -> str:
    """One-line status counts."""
    done = "complete" if manifest["complete"] else "incomplete"
    return (
        f"{manifest['chip']}: {len(manifest['status'])}/{manifest['eraseblocks']} "
        f"eraseblocks {done}; {_counts(manifest['status'])}"
    )
