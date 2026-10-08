"""Restore of a backup onto the fake board."""

# pylint: disable=missing-function-docstring

import json

import pytest
from fakedev import FakeDevice, FakeNand
from flashkit import TOSHIBA

from pocketrechip import backup as B
from pocketrechip import fel_agent
from pocketrechip import restore as R
from pocketrechip.cli import main

SMALL = B.Geometry(page=0x40, pages=4, oob=0x10, eraseblocks=10)


@pytest.fixture(name="backed_up")
def backed_up_fixture(tmp_path, monkeypatch):
    """A complete backup of a fake board with bad eraseblock 5; detection says Toshiba."""
    monkeypatch.setattr(R, "detect_chip", lambda agent: TOSHIBA)
    nand = FakeNand(SMALL, bad={5})
    out = tmp_path / "bk"
    out.mkdir()
    agent = fel_agent.Agent(out / B.WORK_DIR, FakeDevice(nand, stale=0), poll=0)
    with agent.session(tmp_path / "u"):
        job = B.Backup(agent, out, SMALL, 4)
        job.run(job.start(TOSHIBA.name, 0x40, "00", None))
    return nand, out


def scramble(nand):
    for e in range(nand.eraseblocks):
        nand.erase(e)
    nand.write_raw(0, b"\x5a" * 3 * nand.pages * (nand.page + nand.oob))


def run_restore(tmp_path, nand, out, **kw):
    dev = FakeDevice(nand, stale=1)
    agent = fel_agent.Agent(tmp_path / "w", dev, poll=0)
    return R.restore(agent, tmp_path / "u", out, **kw), dev


def test_chunk_step_skips_unread_blocks(tmp_path):
    step = R.chunk_step(SMALL, tmp_path / "raw", 2, 2, [B.RAW_OK] * 3 + [0])
    assert step.commands == (
        "setenv mtdparts nand0:0x100@0x200(blk)",
        "nand erase.part blk",
        "if nand read 0x45000000 blk 0x40; then if nand write.raw.noverify "
        "0x44000000 0x200 0x4; then mw.b 0x43300000 1; fi; fi",
    )
    assert (step.data.offset, step.data.size, step.strict) == (0x280, 0x280, False)
    assert step.checks == ("eraseblock 2", "eraseblock 3")


def test_restore_round_trip(tmp_path, backed_up):
    nand, out = backed_up
    scramble(nand)
    res, dev = run_restore(tmp_path, nand, out, chunk=3, verify=True)
    assert res == {
        "written": 9,
        "skipped": [],
        "failed": [],
        "bad": [5],
        "mismatch": [],
    }
    for e in range(SMALL.eraseblocks):
        pages = range(e * 4, e * 4 + 4)
        if e == 5:
            assert not any(p in nand.raw_written for p in pages)
        else:
            assert all(nand.raw_page(p) == nand.orig_raw_page(p) for p in pages)
    assert dev.state == "fel"


def test_restore_reports_grown_bad_and_mismatch(tmp_path, backed_up):
    nand, out = backed_up
    scramble(nand)
    nand.bad.add(3)
    res, _ = run_restore(tmp_path, nand, out, chunk=4, verify=True)
    assert (res["failed"], res["bad"], res["mismatch"]) == ([3], [5], [3])


def test_restore_skips_unread_blocks_and_checks_chip(tmp_path, backed_up, monkeypatch):
    nand, out = backed_up
    m = B.load_manifest(out)
    m["status"][7] = 0
    B.write_manifest(out, m)
    res, _ = run_restore(tmp_path, nand, out)
    assert res["skipped"] == [7] and res["written"] == 8
    m["chip"] = "Hynix H27UCG8T2ETR"
    B.write_manifest(out, m)
    with pytest.raises(ValueError, match="backup is of a Hynix"):
        run_restore(tmp_path, nand, out)
    (out / B.RAW_FILE).write_bytes(bytes((out / B.RAW_FILE).stat().st_size))
    monkeypatch.setattr(R, "detect_chip", lambda agent: pytest.fail("device used"))
    with pytest.raises(ValueError, match="nand_raw_sha256"):
        run_restore(tmp_path, nand, out)


def test_cli_restore(tmp_path, backed_up, monkeypatch, capsys):
    nand, out = backed_up
    scramble(nand)
    nand.bad.add(2)
    dev = FakeDevice(nand, stale=0)
    monkeypatch.setattr(fel_agent, "subprocess_runner", dev)
    argv = ["restore", "--out", str(tmp_path / "o"), "--backup", str(out)]
    assert main(argv + ["--uboot", str(tmp_path / "u")]) == 1
    assert json.loads(capsys.readouterr().out)["failed"] == [2]
