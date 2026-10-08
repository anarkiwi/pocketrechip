"""Fake release inputs for flash tests."""

import hashlib
import shutil

import numpy as np

from pocketrechip import fel_agent
from pocketrechip import flash as F
from pocketrechip import images as I
from pocketrechip import overlay as O
from pocketrechip import probe_layout as L
from pocketrechip import release as R

TOSHIBA = L.NAND_CHIPS[4 << 30]
HYNIX = L.NAND_CHIPS[8 << 30]
SPL_LEN = 5000
BOOT_SCR = b"boot script " * 40


def prepared(tmp, ubifs_len=300_000, scr=BOOT_SCR) -> F.Prepared:
    """Prepared with a small fake SPL, U-Boot and UBIFS image."""
    tmp.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    files = {
        "spl": rng.bytes(SPL_LEN),
        "uboot": rng.bytes(100_000),
        "img": rng.bytes(ubifs_len),
        "fel": b"release u-boot",
    }
    for k, v in files.items():
        (tmp / k).write_bytes(v)
    (tmp / "boot.scr").write_bytes(scr)
    return F.Prepared(
        "pocketchip",
        tmp / "fel",
        tmp / "spl",
        I.pad_uboot(tmp / "uboot", tmp / "uboot.pad"),
        I.Ubifs(tmp / "img", tmp / "boot.scr"),
    )


def release_env(tmp_path, monkeypatch, dev):
    """Release assets served from file:// and a prebuilt UBIFS in the cache."""
    prep = prepared(tmp_path / "rel")
    src = tmp_path / "srv"
    assets = {}
    for asset, data in (
        (R.SPL, prep.spl.read_bytes()),
        (R.UBOOT_DTB, (tmp_path / "rel" / "uboot").read_bytes()),
        (R.UBOOT_FEL, prep.uboot_fel.read_bytes()),
    ):
        path = src / asset.repo / asset.tag / asset.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        assets[asset.name] = R.Asset(
            asset.repo, asset.tag, asset.name, hashlib.sha256(data).hexdigest()
        )
    monkeypatch.setattr(R, "URL", f"file://{src}/{{repo}}/{{tag}}/{{name}}")
    for attr in ("SPL", "UBOOT_DTB", "UBOOT_FEL"):
        monkeypatch.setattr(R, attr, assets[getattr(R, attr).name])
    cache = tmp_path / "cache"
    tar_sha = R.ROOTFS["pocketchip"].sha256
    base = cache / "ubifs" / I.ubifs_key(tar_sha, O.default())[:16]
    base.parent.mkdir(parents=True)
    shutil.copyfile(prep.ubifs.image, base.with_suffix(".ubifs"))
    shutil.copyfile(prep.ubifs.boot_scr, base.with_suffix(".boot.scr"))
    base.with_suffix(".json").write_text("{}")
    dev.ubi.register(prep.ubifs.image.read_bytes(), {"/boot/boot.scr": BOOT_SCR})
    monkeypatch.setattr(fel_agent, "subprocess_runner", dev)
    return cache
