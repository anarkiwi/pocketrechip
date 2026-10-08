"""Synthetic probe-window builders."""

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from pocketrechip import probe_layout as L

CACHE = Path(__file__).resolve().parents[1] / "cache" / "fel-probe"


class Synth:
    """Builds a probe DRAM window in memory."""

    def __init__(self):
        self.buf = np.zeros(L.WINDOW_LEN, np.uint8)

    def put(self, off: int, data: bytes) -> None:
        """Write bytes at a window offset."""
        self.buf[off : off + len(data)] = np.frombuffer(data, np.uint8)

    def finish(self) -> None:
        """Write the script-finished marker."""
        self.put(L.DONE_OFF, struct.pack("<I", L.DONE_MAGIC))

    def status(self, name: str, value: int) -> None:
        """Set a region status byte."""
        self.buf[L.STATUS_OFF + [r.name for r in L.REGIONS].index(name)] = value

    def region(self, name: str, data: bytes, at: int = 0) -> None:
        """Write data into a region's window slot."""
        self.put(L.REGION[name].win_off + at, data)

    def eb(self, n: int, status: int, page: bytes = b"") -> None:
        """Set eraseblock n's status and first-page bytes (zero padded)."""
        self.buf[L.EB_STATUS_OFF + n] = status
        self.put(L.EB_PAGES_OFF + n * L.PAGE, page.ljust(L.PAGE, b"\x00"))

    @staticmethod
    def ubi(ec: int, seq: int, good: bool = True, version: int = 1) -> bytes:
        """A UBI EC header; good=False corrupts its CRC."""
        hdr = b"UBI#" + struct.pack(">B3xQIII32x", version, ec, 0x4000, 0x8000, seq)
        crc = zlib.crc32(hdr) ^ 0xFFFFFFFF
        return hdr + struct.pack(">I", crc ^ (0 if good else 1))

    @staticmethod
    def env(env: dict[str, str], size: int, flag: int | None = None) -> bytes:
        """A U-Boot env blob; flag set gives the redundant layout."""
        body = b"".join(f"{k}={v}".encode() + b"\x00" for k, v in env.items()) + b"\x00"
        head = b"" if flag is None else bytes([flag])
        body = body.ljust(size - 4 - len(head), b"\x00")
        return struct.pack("<I", zlib.crc32(body)) + head + body


@pytest.fixture(name="synth")
def synth_fixture() -> Synth:
    """Fresh all-zero window."""
    return Synth()


@pytest.fixture(name="cache_dir")
def cache_dir_fixture() -> Path:
    """Gitignored probe artifacts (reference probe.cmd, hardware dram.bin)."""
    return CACHE
