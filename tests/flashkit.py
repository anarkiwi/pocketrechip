"""Fake release inputs for flash tests."""

import numpy as np

from pocketrechip import flash as F
from pocketrechip import images as I
from pocketrechip import probe_layout as L

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
