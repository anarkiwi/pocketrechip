"""Pinned x-chip release assets (anarkiwi forks), downloaded once and sha256-verified."""

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from .backup import HASH_BLOCK, sha256_file
from .progress import progress_bar

log = logging.getLogger(__name__)

UBOOT_TAG = "uboot-2026.10.08-231804"
OS_TAG = "os-2026.10.09-071038"
OWNER = "anarkiwi"
URL = "https://github.com/" + OWNER + "/{repo}/releases/download/{tag}/{name}"


@dataclass(frozen=True)
class Asset:
    """A release asset and its pinned sha256."""

    repo: str
    tag: str
    name: str
    sha256: str

    @property
    def url(self) -> str:
        """Download URL."""
        return URL.format(repo=self.repo, tag=self.tag, name=self.name)

    def path(self, cache: Path) -> Path:
        """Cached location."""
        return Path(cache) / "releases" / self.tag / self.name


def _uboot(name: str, sha: str) -> Asset:
    return Asset("x-chip-uboot", UBOOT_TAG, name, sha)


def _rootfs(flavor: str, sha: str) -> Asset:
    return Asset("x-chip-os", OS_TAG, f"{flavor}-rootfs.tar.gz", sha)


SPL = _uboot(
    "sunxi-spl.bin", "2cdea62d7d3d5fc69f6c1d2a99f0fc0137b361903e3228b2121172aabcfb59cb"
)
UBOOT_DTB = _uboot(
    "u-boot-dtb.bin", "2d0266ab7ca80abfad8d9076e1981c2f40e7cbf97f2d8e15bfedf99df1194316"
)
UBOOT_FEL = _uboot(
    "u-boot-sunxi-with-spl.bin",
    "fece315215b0b90aae40b87a2d11e32387cba6d91f4e0bc32df9bd69300d51a1",
)
ROOTFS = {
    a.name.split("-")[0]: a
    for a in (
        _rootfs(
            "headless",
            "0b842e29c3ef189d6c0bd63643cb05b3915140ebbe918ae970a55d690e33a8a6",
        ),
        _rootfs(
            "gui", "5593fb152e16753caae542485ea6d70e0b650afe0a82a86bf6ee7b84e42956ec"
        ),
        _rootfs(
            "pocketchip",
            "85160bdd037826efd8b7b8b0e8643ec7979b358e23fdda6c6a49da9a8bfd82d0",
        ),
    )
}


class ChecksumError(ValueError):
    """A downloaded asset does not match its pinned sha256."""


def _download(asset: Asset, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    with urlopen(asset.url) as resp, open(part, "wb") as f:
        size = int(resp.headers.get("Content-Length") or 0) or None
        with progress_bar(
            total=size, unit="B", unit_scale=True, unit_divisor=1024, desc=asset.name
        ) as progress:
            while block := resp.read(HASH_BLOCK):
                h.update(block)
                f.write(block)
                progress.update(len(block))
    if h.hexdigest() != asset.sha256:
        part.unlink()
        raise ChecksumError(
            f"{asset.url}: sha256 {h.hexdigest()}, pinned {asset.sha256}"
        )
    os.replace(part, dest)


def fetch(asset: Asset, cache: Path) -> Path:
    """Cached, verified asset; a cached copy that fails verification is fetched again."""
    dest = asset.path(cache)
    if dest.exists():
        if sha256_file(dest) == asset.sha256:
            return dest
        log.warning("%s does not match its pinned sha256; downloading again", dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", asset.url)
    _download(asset, dest)
    return dest
