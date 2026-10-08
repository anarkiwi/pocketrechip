"""Host-built NAND images: BROM SPL eraseblock, padded U-Boot, cached UBIFS rootfs.

Geometry and tool parameters follow x-chip-tools lib-nand.sh and flash-ubi.sh.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import overlay as O
from . import probe_layout as L
from .backup import HASH_BLOCK
from . import fel_agent
from .fel_agent import Runner
from .progress import progress_bar

log = logging.getLogger(__name__)

SNIB = "sunxi-nand-image-builder"
BOOT0_ECC = "64/1024"
BOOT0_USABLE = 1024
SPL_STRIDE = 64
PAGES = L.ERASEBLOCK // L.PAGE

SLC_GROUPS = 2
LEB = L.ERASEBLOCK // SLC_GROUPS - 2 * L.PAGE
MAX_LEB_CNT = 4096
COMPRESSOR = "zlib"
MKFS_ARGS = (
    "-m",
    str(L.PAGE),
    "-e",
    hex(LEB),
    "-c",
    str(MAX_LEB_CNT),
    "-x",
    COMPRESSOR,
)

ROOTFS_OFF = 0x1000000
UBI_RESERVED_PEBS = 2 + 1 + 1
BEB_LIMIT_PER_1024 = 20
BOOT_SCR = "boot/boot.scr"


def snib(src: Path, dst: Path, oob: int, runner: Runner | None = None) -> bytes:
    """Boot0 (BROM) NAND image of src: one page of data + oob per 1 KiB of input."""
    (runner or fel_agent.subprocess_runner)(
        [SNIB, "-c", BOOT0_ECC, "-p", str(L.PAGE), "-o", str(oob)]
        + ["-u", str(BOOT0_USABLE), "-e", str(L.ERASEBLOCK), "-b", "-s"]
        + [str(src), str(dst)]
    )
    return dst.read_bytes()


def spl_image(spl: Path, oob: int, out: Path, runner: Runner | None = None) -> Path:
    """One eraseblock with an SPL copy every SPL_STRIDE pages, random pages between."""
    page = L.PAGE + oob
    one = snib(spl, out.with_suffix(".one"), oob, runner)
    n, rem = divmod(len(one), page)
    if rem or not 0 < n < SPL_STRIDE:
        raise ValueError(f"{spl}: boot0 image of {len(one)} bytes is not 1-63 pages")
    pad_src = out.with_suffix(".pad")
    with open(out, "wb") as f:
        for _ in range(PAGES // SPL_STRIDE):
            pad_src.write_bytes(os.urandom((SPL_STRIDE - n) * BOOT0_USABLE))
            pad = snib(pad_src, out.with_suffix(".pad.nand"), oob, runner)
            if len(pad) != (SPL_STRIDE - n) * page:
                raise ValueError(f"padding image of {len(pad)} bytes")
            f.write(one + pad)
    for p in (out.with_suffix(".one"), pad_src, out.with_suffix(".pad.nand")):
        p.unlink()
    return out


def pad_uboot(src: Path, out: Path) -> Path:
    """U-Boot zero-padded to one eraseblock."""
    data = src.read_bytes()
    if len(data) > L.ERASEBLOCK:
        raise ValueError(f"{src} exceeds one eraseblock")
    out.write_bytes(data.ljust(L.ERASEBLOCK, b"\0"))
    return out


def bad_peb_limit(eraseblocks: int) -> int:
    """UBI's bad-PEB reserve (get_bad_peb_limit) over the whole chip in SLC PEBs."""
    pebs = eraseblocks * SLC_GROUPS
    limit = pebs * BEB_LIMIT_PER_1024 // 1024
    return limit + int(limit * 1024 // BEB_LIMIT_PER_1024 < pebs)


def capacity(eraseblocks: int) -> int:
    """Bytes `ubi createvol rootfs` gets on an all-good chip."""
    pebs = eraseblocks - ROOTFS_OFF // L.ERASEBLOCK
    return (pebs - UBI_RESERVED_PEBS - bad_peb_limit(eraseblocks)) * LEB


@dataclass(frozen=True)
class Ubifs:
    """Cached UBIFS image of a rootfs tar and its /boot/boot.scr."""

    image: Path
    boot_scr: Path

    @property
    def size(self) -> int:
        """Image bytes."""
        return self.image.stat().st_size


def ubifs_key(tar_sha256: str, overlay: Path | None) -> str:
    """Cache key over the tar contents, the overlay and the mkfs.ubifs parameters."""
    text = f"{tar_sha256} {O.digest(overlay)} {' '.join(MKFS_ARGS)}"
    return hashlib.sha256(text.encode()).hexdigest()


def extract(tar: Path, root: Path) -> None:
    """Untar with numeric owners, permissions and device nodes (needs root)."""
    z = ["-z"] if tar.name.endswith(".gz") else []
    with subprocess.Popen(
        ["tar", "-x", *z, "-p", "--numeric-owner", "-C", str(root), "-f", "-"],
        stdin=subprocess.PIPE,
    ) as proc:
        with (
            open(tar, "rb") as f,
            progress_bar(
                total=tar.stat().st_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"extract {tar.name}",
            ) as progress,
        ):
            while block := f.read(HASH_BLOCK):
                proc.stdin.write(block)
                progress.update(len(block))
        proc.stdin.close()
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, proc.args)


def _ubifs_base(tar_sha256: str, overlay: Path | None, cache: Path) -> Path:
    return Path(cache) / "ubifs" / ubifs_key(tar_sha256, overlay)[:16]


def cached_ubifs(tar_sha256: str, overlay: Path | None, cache: Path) -> Ubifs | None:
    """The cached image of a tar with overlay, if built."""
    base = _ubifs_base(tar_sha256, overlay, cache)
    out = Ubifs(base.with_suffix(".ubifs"), base.with_suffix(".boot.scr"))
    return out if base.with_suffix(".json").exists() else None


def build_ubifs(
    tar: Path,
    tar_sha256: str,
    overlay: Path | None,
    cache: Path,
    runner: Runner | None = None,
) -> Ubifs:
    """UBIFS image of tar with overlay applied, built as root once per cache key."""
    if cached := cached_ubifs(tar_sha256, overlay, cache):
        return cached
    base = _ubifs_base(tar_sha256, overlay, cache)
    meta = base.with_suffix(".json")
    out = Ubifs(base.with_suffix(".ubifs"), base.with_suffix(".boot.scr"))
    if os.geteuid():
        raise PermissionError(
            "building the UBIFS image needs root to keep file ownership: "
            "run this step as root in the Docker image"
        )
    base.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pocketrechip-") as tmp:
        root, img = Path(tmp) / "root", Path(tmp) / "rootfs.ubifs"
        root.mkdir()
        extract(tar, root)
        if overlay:
            O.apply(overlay, root)
        scr = root / BOOT_SCR
        if scr.is_symlink() or not scr.is_file():
            raise FileNotFoundError(f"{tar} has no regular /{BOOT_SCR}")
        t0 = time.monotonic()
        log.info("mkfs.ubifs %s", " ".join(MKFS_ARGS))
        (runner or fel_agent.subprocess_runner)(
            ["mkfs.ubifs", *MKFS_ARGS, "-d", str(root), "-o", str(img)]
        )
        log.info("UBIFS %d bytes in %.0f s", img.stat().st_size, time.monotonic() - t0)
        shutil.copyfile(scr, out.boot_scr)
        shutil.copyfile(img, out.image)
    meta.write_text(
        json.dumps(
            {
                "tar": tar.name,
                "tar_sha256": tar_sha256,
                "overlay": str(overlay) if overlay else None,
                "overlay_sha256": O.digest(overlay),
                "mkfs_ubifs": list(MKFS_ARGS),
                "size": out.size,
            },
            indent=1,
        )
        + "\n"
    )
    return out
