"""Flash plan, SPL image, safety refusals and a full flash against the fake board."""

# pylint: disable=missing-function-docstring

import json
import shutil

import pytest
from fakedev import FakeDevice, FakeNand
from flashkit import BOOT_SCR, HYNIX, SPL_LEN, TOSHIBA, prepared, release_env

from pocketrechip import backup as B
from pocketrechip import fel_agent
from pocketrechip import flash as F
from pocketrechip import images as I
from pocketrechip import probe_layout as L
from pocketrechip import steps as S
from pocketrechip.cli import main

GEOM = {c: B.Geometry(L.PAGE, I.PAGES, c.oob, c.eraseblocks) for c in (TOSHIBA, HYNIX)}
CHUNK = 128 * 1024


def fake_spl(tmp_path, chip, prep):
    return I.spl_image(
        prep.spl, chip.oob, tmp_path / f"spl-{chip.key}.nand", FakeDevice(None)
    )


def test_chunks_cover_total_with_partial_tail():
    assert F.chunks(10, 4) == [(0, 4), (4, 4), (8, 2)]
    assert F.chunks(8, 4) == [(0, 4), (4, 4)]
    assert F.chunks(3, 4) == [(0, 3)]


@pytest.mark.parametrize("chip", [TOSHIBA, HYNIX])
def test_spl_image_layout(tmp_path, chip):
    prep = prepared(tmp_path)
    img = fake_spl(tmp_path, chip, prep).read_bytes()
    page = L.PAGE + chip.oob
    assert len(img) == I.PAGES * page
    pages = [img[i : i + page] for i in range(0, len(img), page)]
    spl = prep.spl.read_bytes()
    n = -(-SPL_LEN // 1024)
    for copy in range(0, I.PAGES, I.SPL_STRIDE):
        got = b"".join(p[:1024] for p in pages[copy : copy + n])
        assert got[:SPL_LEN] == spl
        pad = b"".join(p[:1024] for p in pages[copy + n : copy + I.SPL_STRIDE])
        assert len(pad) == (I.SPL_STRIDE - n) * 1024 and len(set(pad)) > 200
    assert all(p[1024:] == b"\xff" * (page - 1024) for p in pages)
    assert sorted(p.name for p in tmp_path.glob("spl-*")) == [f"spl-{chip.key}.nand"]


@pytest.mark.skipif(not shutil.which(I.SNIB), reason="no sunxi-nand-image-builder")
def test_spl_image_real_builder(tmp_path):
    prep = prepared(tmp_path)
    img = I.spl_image(prep.spl, TOSHIBA.oob, tmp_path / "spl.nand").read_bytes()
    page = L.PAGE + TOSHIBA.oob
    n = -(-SPL_LEN // 1024)
    assert len(img) == I.PAGES * page
    for copy in range(I.SPL_STRIDE, I.PAGES, I.SPL_STRIDE):
        assert img[copy * page : (copy + n) * page] == img[: n * page]


def test_spl_image_rejects_oversized_spl(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(bytes(64 * 1024))
    with pytest.raises(ValueError, match="not 1-63 pages"):
        I.spl_image(big, 1280, tmp_path / "o.nand", FakeDevice(None))


def test_pad_uboot(tmp_path):
    prep = prepared(tmp_path)
    data = prep.uboot.read_bytes()
    assert len(data) == L.ERASEBLOCK and data[100_000:] == bytes(L.ERASEBLOCK - 100_000)
    (tmp_path / "huge").write_bytes(bytes(L.ERASEBLOCK + 1))
    with pytest.raises(ValueError, match="one eraseblock"):
        I.pad_uboot(tmp_path / "huge", tmp_path / "x")


def test_ubi_capacity_from_reserves():
    assert I.LEB == 0x1F8000
    assert I.bad_peb_limit(1024) == 40 and I.bad_peb_limit(2048) == 80
    assert I.bad_peb_limit(1000) == 40
    assert I.capacity(1024) == (1020 - 4 - 40) * 0x1F8000
    assert I.capacity(2048) == (2044 - 4 - 80) * 0x1F8000


def test_plan_exact_scripts(tmp_path):
    prep = prepared(tmp_path)
    spl = fake_spl(tmp_path, TOSHIBA, prep)
    steps = F.plan(TOSHIBA, spl, prep, CHUNK)
    assert [s.name for s in steps] == [
        "erase",
        "spl",
        "uboot",
        "ubi",
        "rootfs1of3",
        "rootfs2of3",
        "rootfs3of3",
        "verify",
    ]
    assert [s.checks for s in steps] == [
        ("nand erase.chip",),
        (
            "nand write.raw.noverify 0x4b000000 0x0 0x100",
            "nand write.raw.noverify 0x4b000000 0x400000 0x100",
        ),
        ("nand write 0x4b000000 0x800000 0x400000",),
        ("ubi part rootfs", "ubi createvol rootfs"),
        ("ubi write.part 0x4b000000 rootfs 0x20000 0x493e0",),
        ("ubi write.part 0x4b000000 rootfs 0x20000",),
        ("ubi write.part 0x4b000000 rootfs 0x93e0",),
        (
            "ubifsmount ubi0:rootfs",
            "ubifsload 0x4b000000 /boot/boot.scr",
            "itest ${filesize} == 0x1e0",
        ),
    ]
    assert [(s.data.offset, s.data.size) for s in steps[4:7]] == [
        (0, CHUNK),
        (CHUNK, CHUNK),
        (2 * CHUNK, 300_000 - 2 * CHUNK),
    ]
    scripts = S.render(steps, 1)
    assert scripts[-1] == [
        "mw.b 0x43300000 0 0x3",
        "mw.b 0x4b000000 0 0x1e0",
        "setenv filesize 0",
        S.chain(steps[-1].checks),
        "setenv dfu_alt_info 'cmd ram 0x43200000 0x100000;stat8 ram 0x43300000 0x3;"
        "res8 ram 0x4b000000 0x1e0'",
    ]
    assert scripts[3][-1].endswith(
        "stat4 ram 0x43300000 0x2;img5 ram 0x4b000000 0x20000'"
    )
    assert scripts[0][-1].endswith(f"img2 ram 0x4b000000 {spl.stat().st_size:#x}'")
    text = F.describe(steps)
    assert text.splitlines()[4].split()[:3] == ["5", "rootfs1of3", str(CHUNK)]


def test_plan_bounds(tmp_path):
    prep = prepared(tmp_path)
    spl = fake_spl(tmp_path, TOSHIBA, prep)
    top = F.STAGE_LIMIT - F.STAGE_ADDR
    assert F.STAGE_LIMIT == 0x59800000
    assert F.plan(TOSHIBA, spl, prep, top)[4].checks[0].endswith(" 0x493e0 0x493e0")
    for bad in (0, top + 1):
        with pytest.raises(ValueError, match="staging"):
            F.plan(TOSHIBA, spl, prep, bad)
    with pytest.raises(ValueError, match="not a Hynix"):
        F.plan(HYNIX, spl, prep, CHUNK)
    with open(prep.ubifs.image, "r+b") as f:
        f.truncate(I.capacity(TOSHIBA.eraseblocks) + 1)
    with pytest.raises(ValueError, match="exceeds the Toshiba"):
        F.plan(TOSHIBA, spl, prep, CHUNK)
    assert F.plan(HYNIX, fake_spl(tmp_path, HYNIX, prep), prep, CHUNK)


def make_backup(path, chip=TOSHIBA, complete=True, raw_len=None):
    path.mkdir(parents=True, exist_ok=True)
    g = GEOM[chip]
    m = {
        "chip": chip.name,
        "complete": complete,
        "eraseblocks": 2,
        "raw_eraseblock": g.raw_eraseblock,
    }
    (path / B.MANIFEST).write_text(json.dumps(m))
    with open(path / B.RAW_FILE, "wb") as f:
        f.truncate(2 * g.raw_eraseblock if raw_len is None else raw_len)
    return path


def flash_dev(chip=TOSHIBA, **kw):
    nfc = {TOSHIBA: 0x40, HYNIX: 0x60}[chip]
    return FakeDevice(FakeNand(GEOM[chip]), nfc_id=nfc, stale=1, **kw)


def run_flash(tmp_path, dev, backup=None, register=True, scr=BOOT_SCR):
    prep = prepared(tmp_path / "rel", scr=scr)
    if register:
        dev.ubi.register(prep.ubifs.image.read_bytes(), {"/boot/boot.scr": BOOT_SCR})
    agent = fel_agent.Agent(tmp_path / "w", dev, poll=0)
    return F.flash(agent, tmp_path / "probe.bin", prep, backup, CHUNK), prep


@pytest.mark.parametrize("chip", [TOSHIBA, HYNIX])
def test_full_flash(tmp_path, chip):
    dev = flash_dev(chip)
    got, prep = run_flash(tmp_path, dev, make_backup(tmp_path / "bk", chip))
    assert got == chip
    assert dev.booted == [str(tmp_path / "probe.bin"), str(prep.uboot_fel)]
    nand = dev.nand
    spl = (tmp_path / "w" / "spl.nand").read_bytes()
    page = L.PAGE + chip.oob
    for e in (0, 1):
        assert b"".join(nand.raw_written[e * 256 + i] for i in range(256)) == spl
    assert b"".join(nand.ecc_written[2 * 256 + i] for i in range(256)) == (
        prep.uboot.read_bytes()
    )
    assert len(nand.raw_written) == 512 and len(nand.ecc_written) == 256
    assert len(spl) == 256 * page
    assert nand.erased == set(range(chip.eraseblocks))
    assert bytes(dev.ubi.volume) == prep.ubifs.image.read_bytes()
    assert dev.ubi.calls == [
        (CHUNK, 300_000),
        (CHUNK, None),
        (300_000 - 2 * CHUNK, None),
    ]
    assert dev.state == "fel"
    assert dev.scripts[-1] == fel_agent.script_text(fel_agent.reset_commands())
    resets = [
        s for s in dev.scripts if s == fel_agent.script_text(fel_agent.reset_commands())
    ]
    assert len(resets) == 2


@pytest.mark.parametrize(
    "word",
    [
        "nand erase.chip",
        "nand write.raw.noverify",
        "nand write",
        "ubi part",
        "ubi createvol",
        "ubi write.part",
        "ubifsmount",
        "ubifsload",
        "itest",
    ],
)
def test_failure_at_each_step_aborts_and_resets(tmp_path, word):
    def inject(words):
        return " ".join(words).startswith(word) and not (
            word == "nand write" and words[1] != "write"
        )

    dev = flash_dev(inject=inject)
    with pytest.raises(S.StepFailed, match=f"`{word}"):
        run_flash(tmp_path, dev)
    assert dev.state == "fel"
    assert dev.scripts[-1] == fel_agent.script_text(fel_agent.reset_commands())
    assert f"if {word}" in dev.scripts[-2]


def test_corrupt_stream_fails_mount(tmp_path):
    dev = flash_dev()
    with pytest.raises(S.StepFailed, match="ubifsmount"):
        run_flash(tmp_path, dev, register=False)


def test_boot_scr_content_mismatch(tmp_path):
    dev = flash_dev()
    with pytest.raises(ValueError, match="differs"):
        run_flash(tmp_path, dev, scr=BOOT_SCR.upper())
    assert dev.state == "fel"


def test_refuses_without_complete_backup(tmp_path):
    dev = flash_dev()
    with pytest.raises(FileNotFoundError, match="no complete backup"):
        run_flash(tmp_path, dev, tmp_path / "none")
    with pytest.raises(FileNotFoundError):
        run_flash(tmp_path, dev, make_backup(tmp_path / "inc", complete=False))
    with pytest.raises(ValueError, match="nand.raw is not"):
        run_flash(tmp_path, dev, make_backup(tmp_path / "short", raw_len=5))
    assert not dev.calls


def test_refuses_chip_mismatch_before_writing(tmp_path):
    dev = flash_dev(TOSHIBA)
    with pytest.raises(ValueError, match="of a Hynix.*not Toshiba"):
        run_flash(tmp_path, dev, make_backup(tmp_path / "bk", HYNIX))
    assert dev.booted == [str(tmp_path / "probe.bin")] and dev.state == "fel"
    assert not dev.nand.erased and not dev.nand.raw_written


def test_refuses_unknown_chip_and_release_disagreement(tmp_path, monkeypatch):
    dev = flash_dev(TOSHIBA)
    dev.nfc_id = 0x60
    with pytest.raises(ValueError, match="gives Hynix"):
        run_flash(tmp_path, dev)
    seq = iter([TOSHIBA, HYNIX])
    monkeypatch.setattr(F, "detect_chip", lambda agent: next(seq))
    dev = flash_dev(TOSHIBA)
    with pytest.raises(ValueError, match="release U-Boot detects Hynix"):
        run_flash(tmp_path, dev)
    assert not dev.nand.erased and dev.state == "fel"


def test_detect_chip_unsupported(tmp_path):
    dev = FakeDevice(FakeNand(B.Geometry(L.PAGE, 256, 1280, 4096)), nfc_id=0, stale=0)
    agent = fel_agent.Agent(tmp_path, dev, poll=0)
    agent.boot(tmp_path / "u")
    with pytest.raises(ValueError, match="unsupported NAND"):
        F.detect_chip(agent)


def test_wait_fel_times_out(tmp_path):
    dev = flash_dev()
    agent = fel_agent.Agent(tmp_path, dev, poll=0)
    agent.boot(tmp_path / "u")
    agent.timeout = 0
    with pytest.raises(TimeoutError, match="no FEL device"):
        agent.wait_fel()


def test_dry_run_writes_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(fel_agent, "subprocess_runner", FakeDevice(None))
    prep = prepared(tmp_path / "rel")
    text = F.dry_run(prep, [TOSHIBA, HYNIX], tmp_path / "out", CHUNK)
    assert "300000 bytes, 1 LEBs, 3 chunks of 0x20000" in text
    assert text.count("rootfs3of3") == 2
    for chip in (TOSHIBA, HYNIX):
        cmds = sorted((tmp_path / "out" / f"plan-{chip.key}").glob("*.cmd"))
        assert len(cmds) == 8 and "nand erase.chip" in cmds[0].read_text()
        size = (tmp_path / "out" / f"spl-{chip.key}.nand").stat().st_size
        assert size == 256 * (L.PAGE + chip.oob)


def cli_env(tmp_path, monkeypatch, dev):
    cache = release_env(tmp_path, monkeypatch, dev)
    return [
        "flash",
        "--out",
        str(tmp_path / "out"),
        "--cache",
        str(cache),
        "--uboot",
        str(tmp_path / "probe.bin"),
        "--chunk-mib",
        "1",
    ]


def test_cli_dry_run_touches_no_usb(tmp_path, monkeypatch, capsys):
    dev = flash_dev()
    argv = cli_env(tmp_path, monkeypatch, dev)
    bk = make_backup(tmp_path / "bk", HYNIX)
    assert main(argv + ["--backup", str(bk), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Hynix" in out and "Toshiba" not in out and "rootfs1of1" in out
    assert main(argv + ["--backup", "x", "--no-backup", "--dry-run"]) == 0
    assert "Hynix" in capsys.readouterr().out
    assert (
        main(argv + ["--backup", "x", "--no-backup", "--dry-run", "--chip", "toshiba"])
        == 0
    )
    assert "Hynix" not in capsys.readouterr().out
    tools = {c[0] for c in dev.calls}
    assert tools == {"sunxi-nand-image-builder"}
    with pytest.raises(FileNotFoundError):
        main(argv + ["--backup", str(tmp_path / "none"), "--dry-run"])
    with pytest.raises(OSError, match="pocketchip-rootfs.tar.gz"):
        main(argv + ["--backup", "x", "--no-overlay", "--no-backup", "--dry-run"])


def test_cli_flash(tmp_path, monkeypatch, capsys):
    dev = flash_dev()
    argv = cli_env(tmp_path, monkeypatch, dev)
    assert main(argv + ["--backup", "x", "--no-backup"]) == 0
    assert "Toshiba TC58TEG5DCLTA00: flash complete" in capsys.readouterr().out
    assert dev.state == "fel" and dev.ubi.mounted
