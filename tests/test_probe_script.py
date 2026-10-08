"""Probe script generation."""

# pylint: disable=missing-function-docstring

import re

import pytest
from fakedev import FakeDevice, FakeNand

from pocketrechip import analyze, fel_agent
from pocketrechip import probe_layout as L
from pocketrechip.backup import geometry
from pocketrechip.cli import main
from pocketrechip.probe_script import capture, commands, script

READ_RE = re.compile(
    r"if nand read(\.raw)? (0x[0-9a-f]+) (\w+) (\w+); "
    r"then mw\.b (0x[0-9a-f]+) 1; else mw\.b \5 2; fi"
)


def test_body_matches_hardware_probe_cmd(cache_dir):
    ref = cache_dir / "probe.cmd"
    if not ref.exists():
        pytest.skip("no reference probe.cmd")
    assert commands()[:-1] == ref.read_text().splitlines()[:-3]


def test_reads_are_disjoint_and_inside_window():
    reads = [READ_RE.fullmatch(c) for c in commands() if c.startswith("if ")]
    assert all(reads)
    spans, flags = [], []
    for m in reads:
        size = L.PAGE + L.OOB_MAX if m[1] else int(m[4], 16)
        spans.append((int(m[2], 16), int(m[2], 16) + size))
        flags.append(int(m[5], 16))
    spans.append((L.WINDOW_BASE + L.NFC_OFF, L.WINDOW_BASE + L.NFC_OFF + L.NFC_LEN))
    spans.append((L.WINDOW_BASE, L.WINDOW_BASE + L.CLEAR_LEN))
    spans.sort()
    assert spans[0][0] >= L.WINDOW_BASE and spans[-1][1] <= L.WINDOW_BASE + L.WINDOW_LEN
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert len(set(flags)) == len(flags) == len(L.REGIONS) + L.N_ERASEBLOCKS
    assert all(L.WINDOW_BASE <= f < L.WINDOW_BASE + L.DONE_OFF for f in flags)


def test_eraseblock_reads_cover_first_pages():
    reads = [
        READ_RE.fullmatch(c)
        for c in commands()[1 + len(L.REGIONS) :][: L.N_ERASEBLOCKS]
    ]
    assert [int(m[3], 16) for m in reads] == [
        n * L.ERASEBLOCK for n in range(L.N_ERASEBLOCKS)
    ]


def test_never_mounts_or_writes_nand_and_hands_back_to_agent():
    cmds = commands(seq=7)
    text = "\n".join(cmds)
    assert not re.search(
        r"\b(ubi|erase|write|saveenv|dfu|reset|nand (write|erase|scrub))\b", text
    )
    assert cmds[0] == f"mw.b {L.WINDOW_BASE:#x} 0 {L.CLEAR_LEN:#x}"
    assert cmds[-1] == (
        "setenv dfu_alt_info 'cmd ram 0x43200000 0x100000;"
        f"probe7 ram {L.WINDOW_BASE:#x} {L.WINDOW_LEN:#x}'"
    )
    assert f"mw.l {L.WINDOW_BASE + L.DONE_OFF:#x} {L.DONE_MAGIC:#x}" in cmds
    assert fel_agent.CMD_ADDR + fel_agent.CMD_LEN <= L.WINDOW_BASE


def test_cli_prints_script(capsys):
    assert main(["probe-script", "--seq", "3"]) == 0
    assert capsys.readouterr().out == script(3)


def chip():
    return FakeNand(geometry(0x40))


def test_capture_over_agent(tmp_path):
    nand = chip()
    dev = FakeDevice(nand, stale=2)
    agent = fel_agent.Agent(tmp_path / "work", dev, poll=0)
    with agent.session(tmp_path / "u-boot.bin"):
        dram = capture(agent, tmp_path)
    s = analyze.load(dram)
    assert s.finished and s.nand_size == 4 << 30 and s.chip.startswith("Toshiba")
    assert set(s.statuses.values()) == {"ok", "failed"}
    assert s.block_counts == {
        "not-run": 0,
        "read-failed": 0,
        "erased": 0,
        "ubi": 0,
        "data": 1024,
    }
    buf = dram.read_bytes()
    r = L.REGION["env"]
    assert (
        buf[r.win_off : r.win_off + r.size] == nand.read(r.nand_off, r.size, r.size)[0]
    )
    assert buf[L.REGION["raw_page0"].win_off :][: L.PAGE + 1280] == nand.raw_page(0)
    assert [c[3:5] for c in dev.calls if "-D" in c] == [
        ["-a", "cmd"],
        ["-a", "cmd"],
    ]
    assert dev.scripts == [script(1), "reset\n"]
    assert dev.state == "fel"


def test_cli_probe(tmp_path, monkeypatch, capsys):
    dev = FakeDevice(chip(), stale=0)
    monkeypatch.setattr(fel_agent, "subprocess_runner", dev)
    out = tmp_path / "out"
    assert main(["probe", "--out", str(out), "--uboot", "u.bin", "--json"]) == 0
    assert '"nand_size": 4294967296' in capsys.readouterr().out
    assert (out / "dram.bin").stat().st_size == L.WINDOW_LEN
