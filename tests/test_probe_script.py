"""Probe script generation."""

# pylint: disable=missing-function-docstring

import re

import pytest

from pocketrechip import probe_layout as L
from pocketrechip.cli import main
from pocketrechip.probe_script import commands, script

READ_RE = re.compile(
    r"if nand read(\.raw)? (0x[0-9a-f]+) (\w+) (\w+); "
    r"then mw\.b (0x[0-9a-f]+) 1; else mw\.b \5 2; fi"
)


def test_matches_probe_sh_output(cache_dir):
    ref = cache_dir / "probe.cmd"
    if not ref.exists():
        pytest.skip("no reference probe.cmd")
    assert script() == ref.read_text()


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


def test_never_mounts_or_writes_nand_and_ends_in_dfu_reset():
    cmds = commands(dfu_sessions=2)
    text = "\n".join(cmds)
    assert not re.search(
        r"\b(ubi|erase|write|saveenv|nand (write|erase|scrub))\b", text
    )
    assert cmds[0] == f"mw.b {L.WINDOW_BASE:#x} 0 {L.CLEAR_LEN:#x}"
    assert (
        cmds[-3]
        == f"setenv dfu_alt_info 'probe ram {L.WINDOW_BASE:#x} {L.WINDOW_LEN:#x}'"
    )
    assert cmds[-2] == "dfu 0 ram 0; dfu 0 ram 0"
    assert cmds[-1] == "reset"
    assert f"mw.l {L.WINDOW_BASE + L.DONE_OFF:#x} {L.DONE_MAGIC:#x}" in cmds


def test_cli_prints_script(capsys):
    assert main(["probe-script", "--dfu-sessions", "1"]) == 0
    assert capsys.readouterr().out == script(1)
