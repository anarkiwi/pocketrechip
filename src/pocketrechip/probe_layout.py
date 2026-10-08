"""DRAM window layout written by the FEL NAND probe script."""

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

NAND_CHIPS = {
    0x40: ("Toshiba TC58TEG5DCLTA00", 4 << 30, 1280),
    0x60: ("Hynix H27UCG8T2ETR", 8 << 30, 1664),
}


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
