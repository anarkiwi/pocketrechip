"""Agent loop script and dfu-util host helpers."""

# pylint: disable=missing-function-docstring

import struct
import subprocess

import pytest
from fakedev import FakeDevice, FakeNand

from pocketrechip import fel_agent as A
from pocketrechip import probe_layout as L
from pocketrechip.backup import Geometry

LISTING = """dfu-util 0.11
Found Runtime: [1d6b:0003] ver=0612, devnum=1, cfg=1, intf=0, path="3", alt=0, name="x", serial="y"
Found DFU: [1f3a:1010] ver=0223, devnum=12, cfg=1, intf=0, path="3-2.4", alt=2, name="stat3", serial="UNKNOWN"
Found DFU: [1f3a:1010] ver=0223, devnum=12, cfg=1, intf=0, path="3-2.4", alt=0, name="cmd", serial="UNKNOWN"
Found DFU: [1f3a:1010] ver=0223, devnum=12, cfg=1, intf=0, path="3-2.4", alt=1, name="raw3", serial="UNKNOWN"
Found DFU: [0483:df11] ver=0200, devnum=7, cfg=1, intf=0, path="1-1", alt=0, name="@Flash", serial="1"
"""


def nand():
    return FakeNand(Geometry(0x40, 4, 0x10, 8))


def test_boot_script_loops_forever_over_cmd_entity():
    assert A.boot_commands() == [
        "setenv dfu_alt_info 'cmd ram 0x43200000 0x100000'",
        "while itest 1 == 1; do dfu 0 ram 0; source 0x43200000; "
        "mw.l 0x43200000 0 4; done",
        "reset",
    ]


def test_alt_info_starts_with_cmd():
    assert A.alt_info([("raw3", 0x44000000, 0x100), ("stat3", A.STAT_ADDR, 4)]) == (
        "cmd ram 0x43200000 0x100000;raw3 ram 0x44000000 0x100;stat3 ram 0x43300000 0x4"
    )
    assert A.CMD_ADDR + A.CMD_LEN <= A.STAT_ADDR
    assert L.SCRIPT_ADDR < A.CMD_ADDR


def test_alts_parses_only_agent_dfu_lines_in_alt_order():
    assert A.Dfu(lambda argv: LISTING).alts() == ["cmd", "raw3", "stat3"]


def test_alts_empty_when_dfu_util_fails():
    def runner(argv):
        raise subprocess.CalledProcessError(74, argv)

    assert not A.Dfu(runner).alts()


def test_wait_alt_times_out():
    with pytest.raises(TimeoutError, match="'probe1'"):
        A.Dfu(lambda argv: LISTING, poll=0).wait_alt("probe1", 0)


def test_wait_alt_skips_stale_sessions(tmp_path):
    dev = FakeDevice(nand(), stale=3)
    agent = A.Agent(tmp_path, dev, timeout=5, poll=0)
    agent.boot(tmp_path / "u-boot.bin")
    lists = [c for c in dev.calls if c == ["dfu-util", "-l"]]
    assert len(lists) == 4 and dev.calls[0][0] == "mkimage"
    assert dev.calls[1] == [
        "sunxi-fel",
        "-p",
        "uboot",
        str(tmp_path / "u-boot.bin"),
        "write",
        "0x43100000",
        str(tmp_path / "agent.scr"),
    ]


def test_upload_checks_size(tmp_path):
    def runner(_argv):
        (tmp_path / "x.bin").write_bytes(b"abc")
        return ""

    with pytest.raises(IOError, match="0x3 bytes, expected 0x4"):
        A.Dfu(runner).upload("raw1", tmp_path / "x.bin", 4)


def test_upload_replaces_existing_file(tmp_path):
    calls = []

    def runner(argv):
        calls.append(argv)
        assert not (tmp_path / "x.bin").exists()
        (tmp_path / "x.bin").write_bytes(b"abcd")
        return ""

    (tmp_path / "x.bin").write_bytes(b"old")
    A.Dfu(runner).upload("raw1", tmp_path / "x.bin", 4)
    assert calls == [
        ["dfu-util", "-d", A.DFU_ID, "-a", "raw1", "-U", str(tmp_path / "x.bin")]
        + ["-Z", "4"]
    ]


def test_download_detaches_without_bus_reset(tmp_path):
    calls = []
    A.Dfu(lambda argv: calls.append(argv) or "").download("cmd", tmp_path / "s.scr")
    assert calls == [
        ["dfu-util", "-d", A.DFU_ID, "-a", "cmd", "-D", str(tmp_path / "s.scr")],
        ["dfu-util", "-d", A.DFU_ID, "-a", "cmd", "-e"],
    ]
    assert not any("-R" in c for c in calls)


def test_mkimage_builds_legacy_script_image(tmp_path):
    text = "echo hi\nreset\n"
    img = A.mkimage(text, tmp_path / "t.scr").read_bytes()
    magic, _, _, size = struct.unpack(">IIII", img[:16])
    os_, arch, kind, comp = img[28:32]
    assert (magic, os_, arch, kind, comp) == (0x27051956, 5, 2, 6, 0)
    assert size == len(text) + 8
    assert img[64:72] == struct.pack(">II", len(text), 0)
    assert img[72:].decode() == text
    assert (tmp_path / "t.cmd").read_text() == text


def test_run_rejects_script_larger_than_cmd_entity(tmp_path):
    dev = FakeDevice(nand(), stale=0)
    agent = A.Agent(tmp_path, dev, poll=0)
    agent.boot(tmp_path / "u-boot.bin")
    with pytest.raises(ValueError, match="exceeds the cmd entity"):
        agent.run("big", ["mw.b 0x44000000 0 1"] * (A.CMD_LEN // 16), None)


def test_session_runs_and_resets(tmp_path):
    dev = FakeDevice(nand(), stale=1)
    agent = A.Agent(tmp_path, dev, poll=0)
    with agent.session(tmp_path / "u-boot.bin"):
        seq = agent.next_seq()
        agent.run(
            "t",
            ["mw.b 0x44000000 0x5a 2", A.serve([(f"t{seq}", 0x44000000, 2)])],
            f"t{seq}",
        )
        assert agent.dfu.upload(f"t{seq}", tmp_path / "t.bin", 2).read_bytes() == b"ZZ"
    assert dev.state == "fel" and dev.scripts[-1] == "reset\n"


def test_session_resets_after_error_and_reraises(tmp_path):
    dev = FakeDevice(nand(), stale=0)
    agent = A.Agent(tmp_path, dev, poll=0)
    with pytest.raises(KeyError):
        with agent.session(tmp_path / "u-boot.bin"):
            agent.dfu.upload("nope", tmp_path / "x.bin", 1)
    assert dev.state == "fel"


def test_session_error_survives_failed_reset(tmp_path):
    dev = FakeDevice(nand(), stale=0, fail=lambda argv, n: "reset.scr" in argv[-1])
    agent = A.Agent(tmp_path, dev, poll=0)
    with pytest.raises(RuntimeError, match="boom"):
        with agent.session(tmp_path / "u-boot.bin"):
            raise RuntimeError("boom")
    assert dev.state == "dfu"
