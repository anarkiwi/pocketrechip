"""Decode a DRAM window (dram.bin) captured by the FEL NAND probe."""

import re
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from . import probe_layout as L
from .progress import progress_bar

UBI_EC_MAGIC = b"UBI#"
UBI_EC_DTYPE = np.dtype(
    [
        ("magic", "S4"),
        ("version", "u1"),
        ("pad0", "V3"),
        ("ec", ">u8"),
        ("vid_hdr_offset", ">u4"),
        ("data_offset", ">u4"),
        ("image_seq", ">u4"),
        ("pad1", "V32"),
        ("hdr_crc", ">u4"),
    ]
)
UBI_CRC_LEN = 60

EB_NOT_RUN, EB_FAILED, EB_ERASED, EB_UBI, EB_DATA = range(5)
EB_CLASSES = ("not-run", "read-failed", "erased", "ubi", "data")

VERSION_RE = re.compile(rb"U-Boot \d{4}\.\d{2}[^\x00\n]*")
VERSION_REGIONS = ("uboot", "env", "raw_page0")

ENV_REGION = "env"
ENV_SIZES = tuple(1 << k for k in range(13, L.ERASEBLOCK.bit_length()))
ENV_KEY_RE = re.compile(rb"[A-Za-z0-9_.:\-]+")


@dataclass
class UbiSummary:
    """Aggregate of UBI erase-counter headers found in eraseblock first pages."""

    blocks: int = 0
    crc_ok: int = 0
    crc_bad: int = 0
    versions: list[int] = field(default_factory=list)
    image_seq: dict[int, int] = field(default_factory=dict)
    vid_hdr_offsets: list[int] = field(default_factory=list)
    data_offsets: list[int] = field(default_factory=list)
    ec_min: int | None = None
    ec_max: int | None = None
    ec_mean: float | None = None


@dataclass
class EnvSummary:
    """U-Boot environment found at the start of the env region."""

    crc_ok: bool
    size: int | None
    redundant: bool
    flags: int | None
    vars: dict[str, str]


@dataclass
class Run:
    """Contiguous eraseblocks of one class."""

    start: int
    count: int
    kind: str
    nand_offset: int


@dataclass
class ProbeSummary:
    """Decoded probe window."""

    finished: bool
    statuses: dict[str, str]
    nand_size: int | None
    nfc_id: int
    chip: str | None
    block_counts: dict[str, int]
    ubi: UbiSummary
    uboot_versions: dict[str, list[str]]
    env: EnvSummary | None
    runs: list[Run]


def _region(buf: np.ndarray, name: str) -> np.ndarray:
    r = L.REGION[name]
    return buf[r.win_off : r.win_off + r.size]


def _status(buf: np.ndarray, off: int) -> str:
    return L.STATUS_NAMES.get(int(buf[off]), f"unknown-{int(buf[off])}")


def classify_eraseblocks(buf: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-eraseblock class codes and first-page data (N_ERASEBLOCKS x PAGE)."""
    n, page = L.N_ERASEBLOCKS, L.PAGE
    st = buf[L.EB_STATUS_OFF : L.EB_STATUS_OFF + n]
    pages = buf[L.EB_PAGES_OFF : L.EB_PAGES_OFF + n * page].reshape(n, page)
    erased = (pages.view(np.uint64) == np.uint64(0xFFFFFFFFFFFFFFFF)).all(axis=1)
    ubi = pages[:, :4].copy().view("S4")[:, 0] == UBI_EC_MAGIC
    cls = np.select(
        [st == L.STATUS_OK, st == L.STATUS_FAILED],
        [np.select([erased, ubi], [EB_ERASED, EB_UBI], EB_DATA), EB_FAILED],
        EB_NOT_RUN,
    ).astype(np.uint8)
    return cls, pages


def ubi_summary(headers: np.ndarray) -> UbiSummary:
    """Validate and aggregate raw 64-byte UBI EC headers (rows of uint8)."""
    if headers.shape[0] == 0:
        return UbiSummary()
    hdr = np.ascontiguousarray(headers[:, : UBI_EC_DTYPE.itemsize]).view(UBI_EC_DTYPE)[
        :, 0
    ]
    crc = np.fromiter(
        (
            zlib.crc32(h[:UBI_CRC_LEN].tobytes()) ^ 0xFFFFFFFF
            for h in progress_bar(
                iterable=headers, desc="ubi crc", unit="hdr", leave=False
            )
        ),
        np.uint32,
        len(headers),
    )
    ok = crc == hdr["hdr_crc"]
    good = hdr[ok]
    seq, cnt = np.unique(good["image_seq"], return_counts=True)
    ec = good["ec"]
    return UbiSummary(
        blocks=len(hdr),
        crc_ok=int(ok.sum()),
        crc_bad=int((~ok).sum()),
        versions=np.unique(good["version"]).tolist(),
        image_seq=dict(zip(seq.tolist(), cnt.tolist())),
        vid_hdr_offsets=np.unique(good["vid_hdr_offset"]).tolist(),
        data_offsets=np.unique(good["data_offset"]).tolist(),
        ec_min=int(ec.min()) if len(ec) else None,
        ec_max=int(ec.max()) if len(ec) else None,
        ec_mean=float(ec.mean()) if len(ec) else None,
    )


def runs(cls: np.ndarray) -> list[Run]:
    """Run-length encode eraseblock classes."""
    starts = np.flatnonzero(np.r_[True, cls[1:] != cls[:-1]])
    counts = np.diff(np.r_[starts, len(cls)])
    return [
        Run(int(s), int(c), EB_CLASSES[cls[s]], int(s) * L.ERASEBLOCK)
        for s, c in zip(starts, counts)
    ]


def uboot_versions(buf: np.ndarray) -> dict[str, list[str]]:
    """Unique U-Boot version strings per region, in order of appearance."""
    return {
        name: list(
            dict.fromkeys(
                m.decode("ascii", "replace")
                for m in VERSION_RE.findall(_region(buf, name).tobytes())
            )
        )
        for name in VERSION_REGIONS
    }


def _env_vars(data: bytes) -> dict[str, str]:
    out = {}
    for entry in data.split(b"\x00\x00", 1)[0].split(b"\x00"):
        key, sep, val = entry.partition(b"=")
        if not sep or not ENV_KEY_RE.fullmatch(key):
            break
        out[key.decode()] = val.decode("utf-8", "replace")
    return out


def find_env(region: bytes) -> EnvSummary | None:
    """Locate a U-Boot env (single or redundant layout) at the start of region."""
    crc = int.from_bytes(region[:4], "little")
    for size in ENV_SIZES:
        if size > len(region):
            break
        for start in (4, 5):
            if zlib.crc32(region[start:size]) == crc:
                redundant = start == 5
                return EnvSummary(
                    True,
                    size,
                    redundant,
                    region[4] if redundant else None,
                    _env_vars(region[start:size]),
                )
    limit = region[: ENV_SIZES[-1]]
    best = max(((s, _env_vars(limit[s:])) for s in (4, 5)), key=lambda t: len(t[1]))
    if not best[1]:
        return None
    redundant = best[0] == 5
    return EnvSummary(False, None, redundant, region[4] if redundant else None, best[1])


def analyze(buf: np.ndarray | bytes) -> ProbeSummary:
    """Decode a probe window."""
    buf = np.frombuffer(buf, np.uint8) if isinstance(buf, (bytes, bytearray)) else buf
    if len(buf) < L.WINDOW_LEN:
        raise ValueError(f"window is {len(buf):#x} bytes, expected {L.WINDOW_LEN:#x}")
    statuses = {r.name: _status(buf, L.STATUS_OFF + i) for i, r in enumerate(L.REGIONS)}
    size = L.nand_size([buf[L.STATUS_OFF + L.REGIONS.index(r)] for r in L.SIZE_PROBES])
    nfc_id = int(buf[L.NFC_OFF + L.NFC_ID_BYTE])
    chip = L.NAND_CHIPS.get(size) or L.CHIP_BY_ID.get(nfc_id)
    cls, pages = classify_eraseblocks(buf)
    done = int.from_bytes(buf[L.DONE_OFF : L.DONE_OFF + 4].tobytes(), "little")
    return ProbeSummary(
        finished=done == L.DONE_MAGIC,
        statuses=statuses,
        nand_size=size,
        nfc_id=nfc_id,
        chip=chip.name if chip else None,
        block_counts=dict(
            zip(EB_CLASSES, np.bincount(cls, minlength=len(EB_CLASSES)).tolist())
        ),
        ubi=ubi_summary(pages[cls == EB_UBI, :64]),
        uboot_versions=uboot_versions(buf),
        env=find_env(_region(buf, ENV_REGION).tobytes()),
        runs=runs(cls),
    )


def load(path: str | Path) -> ProbeSummary:
    """Decode a dram.bin file."""
    return analyze(np.fromfile(path, np.uint8))


def to_dict(summary: ProbeSummary) -> dict:
    """JSON-serialisable form of a summary."""
    d = asdict(summary)
    d["ubi"]["image_seq"] = {str(k): v for k, v in d["ubi"]["image_seq"].items()}
    return d


def _size(n: int | None) -> str:
    return "unknown" if n is None else f"{n >> 30} GiB"


def report(s: ProbeSummary) -> str:
    """Compact human-readable report."""
    lines = [
        f"script finished: {'yes' if s.finished else 'NO'}",
        "regions: " + " ".join(f"{k}={v}" for k, v in s.statuses.items()),
        f"nand size: {_size(s.nand_size)}; nfc id byte {s.nfc_id:#04x} ({s.chip or 'unknown'})",
        "eraseblocks: " + " ".join(f"{k}={v}" for k, v in s.block_counts.items() if v),
    ]
    u = s.ubi
    if u.blocks:
        seq = ", ".join(f"{k:#010x} x{v}" for k, v in u.image_seq.items())
        lines.append(f"ubi: {u.blocks} ec headers, crc ok {u.crc_ok} bad {u.crc_bad}")
        if u.crc_ok:
            lines.append(
                f"  image_seq {seq}; ec min {u.ec_min} max {u.ec_max} mean {u.ec_mean:.1f}; "
                f"vid_hdr_offset {u.vid_hdr_offsets} data_offset {u.data_offsets}"
            )
    for name, vers in s.uboot_versions.items():
        for v in vers:
            lines.append(f"{name}: {v}")
    if s.env:
        e = s.env
        crc = f"crc ok size {e.size:#x}" if e.crc_ok else "crc not matched"
        lines.append(
            f"env ({crc}{', redundant' if e.redundant else ''}): {len(e.vars)} vars"
        )
        lines += [f"  {k}={v}" for k, v in e.vars.items()]
    lines.append("layout:")
    lines += [
        f"  eb {r.start:4d}-{r.start + r.count - 1:4d} @ {r.nand_offset:#011x}: {r.kind}"
        for r in s.runs
    ]
    return "\n".join(lines)
