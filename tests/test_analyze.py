"""Probe window decoding."""

# pylint: disable=missing-function-docstring

import json

import numpy as np
import pytest

from pocketrechip import analyze as A
from pocketrechip import probe_layout as L
from pocketrechip.cli import main

OK, FAIL = L.STATUS_OK, L.STATUS_FAILED
ERASED = b"\xff" * L.PAGE


def device(synth):
    """A plausible flashed 4 GiB unit."""
    synth.finish()
    for name in ("uboot", "env", "rootfs_head", "raw_page0"):
        synth.status(name, OK)
    synth.status("probe_4g", FAIL)
    synth.status("probe_8g", FAIL)
    synth.put(L.NFC_OFF + L.NFC_ID_BYTE, b"\x40")
    synth.region("uboot", b"\x00U-Boot 2016.01-00088-gabc (Dec 09 2016)\x00junk")
    synth.region("uboot", b"U-Boot 2016.01-00088-gabc (Dec 09 2016)\n", at=0x1000)
    synth.region("raw_page0", b"xxU-Boot 2016.01 SPL\x00")
    synth.region(
        "env", synth.env({"bootdelay": "1", "bootargs": "root=ubi0:rootfs"}, 0x400000)
    )
    for n in (0, 1):
        synth.eb(n, FAIL)
    synth.eb(2, OK, b"\x12" * 64)
    synth.eb(3, OK, b"\x34" * 64)
    for n in range(4, 1020):
        synth.eb(n, OK, synth.ubi(ec=n % 7, seq=0xA07DB89))
    synth.eb(500, OK, synth.ubi(ec=999, seq=0xA07DB89, good=False))
    synth.eb(501, OK, synth.ubi(ec=3, seq=0x1234))
    for n in range(1020, 1024):
        synth.eb(n, FAIL)
    return synth


def test_device_summary(synth):
    s = A.analyze(device(synth).buf)
    assert s.finished
    assert s.statuses["probe_4g"] == "failed" and s.statuses["uboot"] == "ok"
    assert s.nand_size == 4 << 30
    assert (s.nfc_id, s.chip) == (0x40, "Toshiba TC58TEG5DCLTA00")
    assert s.block_counts == {
        "not-run": 0,
        "read-failed": 6,
        "erased": 0,
        "ubi": 1016,
        "data": 2,
    }
    assert (s.ubi.blocks, s.ubi.crc_ok, s.ubi.crc_bad) == (1016, 1015, 1)
    assert s.ubi.image_seq == {0x1234: 1, 0xA07DB89: 1014}
    assert (s.ubi.ec_min, s.ubi.ec_max) == (0, 6)
    good = [n % 7 for n in range(4, 1020) if n not in (500, 501)] + [3]
    assert s.ubi.ec_mean == pytest.approx(np.mean(good))
    assert (s.ubi.vid_hdr_offsets, s.ubi.data_offsets, s.ubi.versions) == (
        [0x4000],
        [0x8000],
        [1],
    )
    assert s.uboot_versions == {
        "uboot": ["U-Boot 2016.01-00088-gabc (Dec 09 2016)"],
        "env": [],
        "raw_page0": ["U-Boot 2016.01 SPL"],
    }
    assert s.env.crc_ok and s.env.size == 0x400000 and not s.env.redundant
    assert s.env.vars == {"bootdelay": "1", "bootargs": "root=ubi0:rootfs"}
    assert [(r.start, r.count, r.kind, r.nand_offset) for r in s.runs] == [
        (0, 2, "read-failed", 0),
        (2, 2, "data", 0x800000),
        (4, 1016, "ubi", 0x1000000),
        (1020, 4, "read-failed", 0xFF000000),
    ]


def test_empty_window(synth):
    s = A.analyze(bytes(synth.buf))
    assert not s.finished
    assert set(s.statuses.values()) == {"not-run"}
    assert s.nand_size is None and s.chip is None and s.env is None
    assert s.block_counts["not-run"] == L.N_ERASEBLOCKS
    assert s.ubi == A.UbiSummary()
    assert [(r.start, r.count) for r in s.runs] == [(0, L.N_ERASEBLOCKS)]
    assert "script finished: NO" in A.report(s)


@pytest.mark.parametrize(
    "p4,p8,size",
    [
        (OK, FAIL, 8 << 30),
        (FAIL, OK, 4 << 30),
        (OK, OK, None),
        (OK, 0, None),
        (0, FAIL, None),
    ],
)
def test_nand_size(synth, p4, p8, size):
    synth.status("probe_4g", p4)
    synth.status("probe_8g", p8)
    assert A.analyze(synth.buf).nand_size == size


def test_unknown_status_value(synth):
    synth.status("uboot", 7)
    assert A.analyze(synth.buf).statuses["uboot"] == "unknown-7"


def test_erased_and_all_bad_ubi(synth):
    synth.eb(0, OK, ERASED)
    synth.eb(1, OK, ERASED[:-1] + b"\xfe")
    synth.eb(2, OK, synth.ubi(5, 1, good=False))
    s = A.analyze(synth.buf)
    assert [r.kind for r in s.runs[:3]] == ["erased", "data", "ubi"]
    assert (s.ubi.blocks, s.ubi.crc_ok, s.ubi.ec_min, s.ubi.image_seq) == (
        1,
        0,
        None,
        {},
    )


def test_redundant_env(synth):
    synth.region("env", synth.env({"a": "1", "b": "x=y"}, 0x4000, flag=1))
    e = A.analyze(synth.buf).env
    assert (e.crc_ok, e.size, e.redundant, e.flags) == (True, 0x4000, True, 1)
    assert e.vars == {"a": "1", "b": "x=y"}


def test_env_without_crc_match(synth):
    blob = bytearray(synth.env({"bootcmd": "run x", "k": ""}, 0x2000))
    blob[0] ^= 0xFF
    synth.region("env", bytes(blob))
    e = A.analyze(synth.buf).env
    assert (e.crc_ok, e.size, e.redundant) == (False, None, False)
    assert e.vars == {"bootcmd": "run x", "k": ""}


def test_env_vars_stop_at_garbage():
    assert not A.find_env(b"\x00" * 8 + b"not an env")
    assert A.find_env(b"\x00" * 4 + b"a=1\x00\xff\xffjunk=2\x00\x00").vars == {"a": "1"}


def test_short_window_rejected():
    with pytest.raises(ValueError, match="expected"):
        A.analyze(b"\x00" * 16)


def test_cli_report_and_json(synth, tmp_path, capsys):
    path = tmp_path / "dram.bin"
    device(synth).buf.tofile(path)
    assert main(["analyze", str(path)]) == 0
    out = capsys.readouterr().out
    for line in (
        "nand size: 4 GiB",
        "image_seq 0x00001234 x1, 0x0a07db89 x1014",
        "  eb    4-1019 @ 0x001000000: ubi",
        "  bootdelay=1",
    ):
        assert line in out
    assert main(["analyze", str(path), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["ubi"]["image_seq"] == {str(0x1234): 1, str(0xA07DB89): 1014}
    assert d["env"]["vars"]["bootdelay"] == "1"
    synth.put(L.DONE_OFF, b"\x00" * 4)
    synth.buf.tofile(path)
    assert main(["analyze", str(path)]) == 1


def test_real_dump(cache_dir):
    path = cache_dir / "dram.bin"
    if not path.exists():
        pytest.skip("no hardware dump")
    s = A.load(path)
    assert s.finished
    assert list(s.statuses.values()) == ["ok", "ok", "ok", "failed", "failed", "ok"]
    assert (s.nfc_id, s.nand_size) == (0x40, 4 << 30)
    assert [(r.start, r.count, r.kind) for r in s.runs] == [
        (0, 2, "read-failed"),
        (2, 2, "data"),
        (4, 1016, "ubi"),
        (1020, 4, "read-failed"),
    ]
    assert s.ubi.crc_ok == 1016 and s.ubi.image_seq == {0xA07DB89: 1016}
    assert (s.ubi.ec_min, s.ubi.ec_max) == (0, 52)
    assert s.uboot_versions["uboot"][0] == "U-Boot 2016.01-00088-g99c771f"
    assert s.env.crc_ok and s.env.size == L.ERASEBLOCK and "bootcmd" in s.env.vars
