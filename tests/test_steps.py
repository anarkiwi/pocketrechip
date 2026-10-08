"""Verified step scripts: chaining, entity hand-over, status checks."""

# pylint: disable=missing-function-docstring

import pytest
from fakedev import FakeDevice, FakeNand

from pocketrechip import fel_agent as A
from pocketrechip import steps as S
from pocketrechip.backup import Geometry


def test_chain_nests_and_marks_each_success():
    assert S.chain(["a 1", "b"], 0x100) == (
        "if a 1; then mw.b 0x100 1; if b; then mw.b 0x101 1; fi; fi"
    )
    assert S.chain(["x"]) == "if x; then mw.b 0x43300000 1; fi"


def test_render_serves_status_result_and_next_data(tmp_path):
    data = tmp_path / "d.bin"
    data.write_bytes(b"0123456789")
    steps = [
        S.chained("a", ["c1", "c2"], prelude=("p",)),
        S.chained("b", ["c3"], data=S.Data(data, 2, 5), addr=0x45000000, result=3),
    ]
    assert S.render(steps, 7) == [
        [
            "mw.b 0x43300000 0 0x2",
            "p",
            S.chain(["c1", "c2"]),
            "setenv dfu_alt_info 'cmd ram 0x43200000 0x100000;"
            "stat7 ram 0x43300000 0x2;img8 ram 0x45000000 0x5'",
        ],
        [
            "mw.b 0x43300000 0 0x1",
            S.chain(["c3"]),
            "setenv dfu_alt_info 'cmd ram 0x43200000 0x100000;"
            "stat8 ram 0x43300000 0x1;res8 ram 0x45000000 0x3'",
        ],
    ]
    with pytest.raises(ValueError, match="first step b"):
        S.render(steps[1:], 1)


def test_data_slices_and_whole_files(tmp_path):
    src = tmp_path / "s.bin"
    src.write_bytes(bytes(range(100)))
    assert S.Data.file(src).materialize(tmp_path / "t") == src
    out = S.Data(src, 90, 10).materialize(tmp_path / "t")
    assert out.read_bytes() == bytes(range(90, 100))
    with pytest.raises(IOError, match="ends before 101"):
        S.Data(src, 91, 10).materialize(tmp_path / "t")


def test_write_scripts(tmp_path):
    paths = S.write_scripts([S.chained("x", ["c"])], tmp_path / "p", 3)
    assert [p.name for p in paths] == ["0003-x.cmd"]
    assert paths[0].read_text().splitlines()[1] == S.chain(["c"])


def agent_dev(tmp_path, **kw):
    dev = FakeDevice(FakeNand(Geometry(0x40, 4, 0x10, 8)), stale=1, **kw)
    agent = A.Agent(tmp_path / "w", dev, poll=0)
    agent.boot(tmp_path / "u.bin")
    return agent, dev


def test_run_downloads_data_and_uploads_results(tmp_path):
    agent, _ = agent_dev(tmp_path)
    src = tmp_path / "d.bin"
    src.write_bytes(b"abcdefgh")
    addr = 0x45000000
    steps = [
        S.chained("init", [f"mw.b {addr:#x} 0 8"]),
        S.chained(
            "copy",
            [f"cp.l {addr:#x} {addr + 8:#x} 1"],
            data=S.Data(src, 2, 4),
            addr=addr,
            result=12,
        ),
    ]
    out = S.run(agent, steps)
    assert [o.status for o in out] == [b"\x01", b"\x01"]
    assert out[1].result == b"cdef\0\0\0\0cdef"
    assert agent.seq == 2 and not list((tmp_path / "w").glob("*.bin"))


def test_strict_step_failure_names_command(tmp_path):
    agent, dev = agent_dev(tmp_path, inject=lambda w: w[0] == "itest")
    steps = [S.chained("s", ["mw.b 0x45000000 1", "itest 1 == 1", "mw.b 0x45000001 1"])]
    with pytest.raises(
        S.StepFailed, match=r"step s: `itest 1 == 1` failed .*\[1, 0, 0\]"
    ):
        S.run(agent, steps)
    assert dev.mem.read(0x45000000, 2) == b"\x01\x00"
    lax = [S.Step("s", ("mw.b 0x43300000 2 1",), ("x",), strict=False)]
    assert S.run(agent, lax)[0].status == b"\x02"
