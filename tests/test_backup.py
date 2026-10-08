"""Full NAND backup: chunk scripts, host flow, resume, verify, manifest."""

# pylint: disable=missing-function-docstring

import hashlib
import json
import re
import subprocess

import numpy as np
import pytest
from fakedev import FakeDevice, FakeNand

from pocketrechip import backup as B
from pocketrechip import fel_agent
from pocketrechip import probe_layout as L
from pocketrechip.cli import main

SMALL = B.Geometry(page=0x40, pages=4, oob=0x10, eraseblocks=10)
TOSHIBA = B.Geometry(L.PAGE, 256, 1280, 1024)
HYNIX = B.Geometry(L.PAGE, 256, 1664, 2048)
OK = B.RAW_OK | B.ECC_OK


def small_nand(**kw):
    return FakeNand(SMALL, **kw)


def test_chunk_commands_exact():
    assert B.chunk_commands(SMALL, 3, 2, 5) == [
        "mw.b 0x43300000 0 0x2",
        "setenv mtdids nand0=nand0",
        "mw.l 0x44000000 0 0xa0",
        "mw.l 0x45000000 0 0x80",
        "if nand read.raw 0x44000000 0x300 0x4; then mw.b 0x43300000 1; fi",
        "setenv mtdparts nand0:0x100@0x300(blk)",
        "if nand read 0x45000000 blk 0x100; then if itest.b *0x43300000 == 1; "
        "then mw.b 0x43300000 3; else mw.b 0x43300000 2; fi; fi",
        "if nand read.raw 0x44000140 0x400 0x4; then mw.b 0x43300001 1; fi",
        "setenv mtdparts nand0:0x100@0x400(blk)",
        "if nand read 0x45000100 blk 0x100; then if itest.b *0x43300001 == 1; "
        "then mw.b 0x43300001 3; else mw.b 0x43300001 2; fi; fi",
        "setenv dfu_alt_info 'cmd ram 0x43200000 0x100000;stat5 ram 0x43300000 0x2;"
        "raw5 ram 0x44000000 0x280;ecc5 ram 0x45000000 0x200'",
    ]


def test_ecc_only_chunk_has_no_raw_entity():
    cmds = B.chunk_commands(SMALL, 0, 1, 2, raw=False)
    assert not any("raw" in c for c in cmds)
    assert cmds[-1].endswith("stat2 ram 0x43300000 0x1;ecc2 ram 0x45000000 0x100'")


@pytest.mark.parametrize("geom,largest", [(TOSHIBA, 37), (HYNIX, 36)])
def test_chunk_bounds(geom, largest):
    for n in (0, largest + 1):
        with pytest.raises(ValueError, match="ends at"):
            B.chunk_commands(geom, 0, n, 1)
    for n in (1, 16, largest):
        cmds = B.chunk_commands(geom, 0, n, 1)
        ents = [e.split() for e in cmds[-1].split("'")[1].split(";")]
        spans = sorted((int(a, 16), int(a, 16) + int(s, 16)) for _, _, a, s in ents)
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
        assert spans[-1][1] <= B.DRAM_LIMIT
        assert [n_ for n_, *_ in ents] == ["cmd", "stat1", "raw1", "ecc1"]
        ecc = int(ents[3][2], 16)
        assert ecc % B.ECC_ALIGN == 0 and ecc - B.ECC_ALIGN < spans[2][1] <= ecc


def test_chunk_never_writes_nand():
    text = "\n".join(B.chunk_commands(TOSHIBA, 0, 16, 1))
    assert not re.search(r"\b(ubi|erase|write|saveenv|scrub|dfu |reset)\b", text)
    assert re.findall(r"nand (\S+)", text) == ["read.raw", "read"] * 16


def test_geometry_from_nfc_id():
    assert B.geometry(0x40) == TOSHIBA
    assert B.geometry(0x60) == HYNIX
    assert B.geometry(0x40, oob=1664, eraseblocks=4) == B.Geometry(L.PAGE, 256, 1664, 4)
    with pytest.raises(ValueError, match="0x13"):
        B.geometry(0x13, oob=1280)
    assert B.geometry(0x13, 1280, 1024) == TOSHIBA


def run_backup(tmp_path, dev, chunk=4, verify=False, out=None):
    out = out or tmp_path / "out"
    out.mkdir(exist_ok=True)
    agent = fel_agent.Agent(out / B.WORK_DIR, dev, poll=0)
    prior = B.load_manifest(out)
    with agent.session(tmp_path / "u-boot.bin"):
        job = B.Backup(agent, out, SMALL, chunk)
        return job.run(job.start(0x40, "ab" * 32, prior), verify), out


def expected(nand, n):
    raw = b"".join(nand.raw_eraseblock(e) for e in range(n))
    ecc = b"".join(
        bytes(SMALL.eraseblock) if e in nand.bad else nand.ecc_eraseblock(e)
        for e in range(n)
    )
    return raw, ecc


def test_backup_flow(tmp_path):
    nand = small_nand(bad={5}, ecc_fail={2})
    dev = FakeDevice(nand, stale=2)
    m, out = run_backup(tmp_path, dev)
    raw, ecc = expected(nand, SMALL.eraseblocks)
    assert (out / B.RAW_FILE).read_bytes() == raw
    assert (out / B.ECC_FILE).read_bytes() == ecc
    assert m["status"] == [OK, OK, B.RAW_OK, OK, OK, B.RAW_OK, OK, OK, OK, OK]
    assert m["complete"] and m["next_eraseblock"] == SMALL.eraseblocks
    assert m["nand_raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert m["nand_ecc_sha256"] == hashlib.sha256(ecc).hexdigest()
    assert m["ecc_sha256"][7] == hashlib.sha256(nand.ecc_eraseblock(7)).hexdigest()
    assert (m["chip"], m["oob"], m["raw_eraseblock"]) == (
        "Toshiba TC58TEG5DCLTA00",
        0x10,
        0x140,
    )
    assert json.loads((out / B.MANIFEST).read_text()) == m
    chunks = [s for s in dev.scripts if "dfu_alt_info" in s]
    assert [re.findall(r"read\.raw \S+ (\S+)", s)[0] for s in chunks] == [
        "0x0",
        "0x400",
        "0x800",
    ]
    assert dev.scripts[-1] == "reset\n" and dev.state == "fel"
    assert not list((out / B.WORK_DIR).glob("*.bin"))


def test_resume_after_failure(tmp_path):
    nand = small_nand()
    uploads = []

    def fail(argv, _):
        if "-U" not in argv:
            return False
        uploads.append(argv)
        return len(uploads) == 7

    dev = FakeDevice(nand, fail=fail)
    with pytest.raises(subprocess.CalledProcessError):
        run_backup(tmp_path, dev)
    assert dev.state == "fel"
    out = tmp_path / "out"
    m = B.load_manifest(out)
    assert (m["next_eraseblock"], m["complete"], len(m["status"])) == (8, False, 8)
    with open(out / B.RAW_FILE, "ab") as f:
        f.write(b"partial")
    dev = FakeDevice(nand)
    m, _ = run_backup(tmp_path, dev)
    raw, ecc = expected(nand, SMALL.eraseblocks)
    assert (out / B.RAW_FILE).read_bytes() == raw
    assert (out / B.ECC_FILE).read_bytes() == ecc
    assert m["complete"] and m["status"] == [OK] * SMALL.eraseblocks
    assert [re.findall(r"read\.raw \S+ (\S+)", s)[0] for s in dev.scripts[:-1]] == [
        "0x800"
    ]


def test_resume_refuses_mismatch_and_orphans(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    agent = fel_agent.Agent(out / B.WORK_DIR, FakeDevice(small_nand()), poll=0)
    job = B.Backup(agent, out, SMALL, 4)
    m = job.start(0x40, "00", None)
    m["oob"] = 0x20
    with pytest.raises(ValueError, match=r"\['oob', 'uboot_sha256'\]"):
        job.start(0x40, "11", m)
    (out / B.RAW_FILE).write_bytes(b"x")
    with pytest.raises(FileExistsError):
        job.start(0x40, "00", None)
    m["oob"] = 0x10
    m["next_eraseblock"] = 1
    with pytest.raises(ValueError, match="shorter"):
        job.run(job.start(0x40, "00", m))


def test_verify_marks_stable_and_unstable_blocks(tmp_path):
    nand = small_nand(bad={1}, flaky={6})
    m, _ = run_backup(tmp_path, FakeDevice(nand), chunk=5, verify=True)
    v = OK | B.VERIFIED
    assert m["status"] == [v, B.RAW_OK, v, v, v, v, OK | B.MISMATCH, v, v, v]
    assert "verified=8 mismatch=1" in B.summary(m)


def test_cli_backup_real_geometry(tmp_path, monkeypatch, capsys):
    nand = FakeNand(TOSHIBA)
    dev = FakeDevice(nand, stale=0)
    monkeypatch.setattr(fel_agent, "subprocess_runner", dev)
    uboot = tmp_path / "u-boot.bin"
    uboot.write_bytes(b"uboot")
    out = tmp_path / "out"
    argv = ["backup", "--out", str(out), "--uboot", str(uboot), "--eraseblocks", "2"]
    assert main(argv + ["--chunk-ebs", "1"]) == 0
    assert "2/2 eraseblocks complete; raw=2 ecc=2" in capsys.readouterr().out
    m = B.load_manifest(out)
    assert m["uboot_sha256"] == hashlib.sha256(b"uboot").hexdigest()
    assert (m["oob"], m["eraseblocks"], m["chunk_eraseblocks"]) == (1280, 2, 1)
    raw = np.fromfile(out / B.RAW_FILE, np.uint8).reshape(2 * 256, L.PAGE + 1280)
    assert raw[300].tobytes() == nand.raw_page(300)
    calls = len(dev.calls)
    assert main(argv) == 0
    assert len(dev.calls) == calls
