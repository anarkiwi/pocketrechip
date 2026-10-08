"""DRAM window layout written by the FEL NAND probe script."""

from collections.abc import Sequence
from dataclasses import dataclass

SCRIPT_ADDR = 0x43100000
WINDOW_BASE = 0x44000000
WINDOW_LEN = 0x2400000

PAGE = 0x4000
ERASEBLOCK = 0x400000
OOB_MAX = 0x680
N_ERASEBLOCKS = 1024

STATUS_OFF = 0x0
EB_STATUS_OFF = 0x100
DONE_OFF = 0xFFC
DONE_MAGIC = 0x45464F44
CLEAR_LEN = 0x1000

NFC_BASE = 0x01C03000
NFC_OFF = 0x100000
NFC_LEN = 0x100
NFC_ID_BYTE = 0x35

EB_PAGES_OFF = 0x1400000

STATUS_NOT_RUN, STATUS_OK, STATUS_FAILED = 0, 1, 2
STATUS_NAMES = {STATUS_NOT_RUN: "not-run", STATUS_OK: "ok", STATUS_FAILED: "failed"}


@dataclass(frozen=True)
class Chip:
    """A supported NAND part."""

    name: str
    nfc_id: int
    size: int
    oob: int

    @property
    def eraseblocks(self) -> int:
        """Eraseblocks on the chip."""
        return self.size // ERASEBLOCK

    @property
    def key(self) -> str:
        """Short lowercase vendor name."""
        return self.name.split()[0].lower()


NAND_CHIPS = {
    c.size: c
    for c in (
        Chip("Toshiba TC58TEG5DCLTA00", 0x40, 4 << 30, 1280),
        Chip("Hynix H27UCG8T2ETR", 0x60, 8 << 30, 1664),
    )
}
CHIP_BY_ID = {c.nfc_id: c for c in NAND_CHIPS.values()}
CHIP_BY_KEY = {c.key: c for c in NAND_CHIPS.values()}


@dataclass(frozen=True)
class Region:
    """A NAND range copied into the window; raw regions read one page plus OOB."""

    name: str
    nand_off: int
    size: int
    win_off: int
    raw: bool = False

    @property
    def addr(self) -> int:
        """Absolute DRAM destination address."""
        return WINDOW_BASE + self.win_off


REGIONS = (
    Region("uboot", 0x800000, 0x400000, 0x200000),
    Region("env", 0xC00000, 0x400000, 0x600000),
    Region("rootfs_head", 0x1000000, 0x800000, 0xA00000),
    Region("probe_4g", 4 << 30, PAGE, 0x1200000),
    Region("probe_8g", 8 << 30, PAGE, 0x1240000),
    Region("raw_page0", 0, PAGE + OOB_MAX, 0x1300000, raw=True),
)
REGION = {r.name: r for r in REGIONS}
SIZE_PROBES = (REGION["probe_4g"], REGION["probe_8g"])
SIZE_PROBE_OFFSETS = tuple(r.nand_off for r in SIZE_PROBES)


def nand_size(probe_status: Sequence[int]) -> int | None:
    """Chip size: the lowest size-probe offset that failed after all lower ones read ok."""
    for off, st in zip(SIZE_PROBE_OFFSETS, probe_status):
        if st == STATUS_FAILED:
            return off
        if st != STATUS_OK:
            return None
    return None


def identify(size: int | None, nfc_id: int) -> Chip | None:
    """Chip for a probed size, cross-checked with the NAND ID byte when non-zero."""
    chip = NAND_CHIPS.get(size)
    by_id = CHIP_BY_ID.get(nfc_id)
    if chip and nfc_id and by_id is not chip:
        other = by_id.name if by_id else "an unknown chip"
        raise ValueError(
            f"size probes give {chip.name} ({size >> 30} GiB) "
            f"but NAND ID byte {nfc_id:#04x} gives {other}"
        )
    return chip
