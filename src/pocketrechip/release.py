"""Pinned NextThingCo release assets, downloaded once into the cache and sha256-verified."""

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from .backup import HASH_BLOCK, sha256_file
from .progress import progress_bar

log = logging.getLogger(__name__)

UBOOT_TAG = "uboot-2026.09.13-122745"
OS_TAG = "os-2026.09.23-010738"
URL = "https://github.com/NextThingCo/{repo}/releases/download/{tag}/{name}"


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
    "sunxi-spl.bin", "879cff4d6345a12091fa8084bab5a002556989b2d10ce1898905dd8666729ba0"
)
UBOOT_DTB = _uboot(
    "u-boot-dtb.bin", "f64e582f7d01cb151cb73f8188ab5d48b5fbc351c9cf43a39570dfc1b48c6aa6"
)
UBOOT_FEL = _uboot(
    "u-boot-sunxi-with-spl.bin",
    "23d2730799a109946753147413153687877e41265981833c4466d034d2279d05",
)
ROOTFS = {
    a.name.split("-")[0]: a
    for a in (
        _rootfs(
            "headless",
            "b4dc1e07c7aad56b7e204699d23660ab13dceed46013b88bf5f654f308783d77",
        ),
        _rootfs(
            "gui", "423edd7f3a07d28a23e38fa401250a70101746ccabc11677cc4b604896896054"
        ),
        _rootfs(
            "pocketchip",
            "1e516cade3085633f61697d69a5d95cb84a501d8b606247987db5837a53e19ef",
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
