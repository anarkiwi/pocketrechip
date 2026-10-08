"""One-command install against the fake board: backup, resume, per-SID state, Wi-Fi."""

# pylint: disable=missing-function-docstring

import hashlib
import io
import os
import re
import stat
import tarfile
from pathlib import Path

import pytest
from fakedev import SID, FakeDevice, FakeNand
from flashkit import BOOT_SCR, TOSHIBA, release_env

from pocketrechip import backup as B
from pocketrechip import fel_agent
from pocketrechip import install as I
from pocketrechip import probe_layout as L
from pocketrechip import release as R
from pocketrechip import wifi as W
from pocketrechip.cli import main

DIR = "02c00081-4c4d4e4f-50515253-54555657"
PASSWORD = "correct horse battery"


@pytest.fixture(name="env")
def env_fixture(tmp_path, monkeypatch):
    """Fake Toshiba board in FEL, release in the cache, backups of two eraseblocks."""
    dev = FakeDevice(FakeNand(B.Geometry(L.PAGE, 256, TOSHIBA.oob, 1024)), stale=0)
    cache = release_env(tmp_path, monkeypatch, dev)
    full = B.geometry
    monkeypatch.setattr(B, "geometry", lambda size, nfc, *_: full(size, nfc, None, 2))
    monkeypatch.delenv(I.HOST_CACHE, raising=False)
    uboot = tmp_path / "probe.bin"
    uboot.write_bytes(b"probe u-boot")
    argv = ["install", "--cache", str(cache), "--uboot", str(uboot)]
    return dev, cache, argv + ["--chunk-ebs", "1", "--timeout", "5"]


def backups(dev):
    """NAND offsets of the backup chunk scripts the board ran."""
    return [
        m
        for s in dev.scripts
        for m in re.findall(r"nand read\.raw \S+ (\S+)", s)
        if "raw" in s
    ]


def test_fresh_device_backs_up_then_flashes(env, capsys):
    dev, cache, argv = env
    assert main(argv) == 0
    out = capsys.readouterr().out
    bdir = cache / I.DEVICES / DIR / I.BACKUP
    m = B.load_manifest(bdir)
    assert m["complete"] and m["chip"] == TOSHIBA.name and m["eraseblocks"] == 2
    assert backups(dev) == ["0x0", "0x400000"]
    probe = str(argv[argv.index("--uboot") + 1])
    assert dev.booted == [probe, probe, str(R.UBOOT_FEL.path(cache))]
    assert dev.ubi.mounted and dev.state == "fel"
    assert [l for l in out.splitlines() if l.startswith("[")] == [
        "[1/4] Image",
        "[2/4] Connect",
        "[3/4] Backup",
        "[4/4] Flash",
    ]
    assert "Jumper the FEL pad to the GROUND pad" in out
    assert f"found SID {DIR}" in out and "2/2 eraseblocks complete" in out
    assert re.search(r"Done in \d\d:\d\d \(image \d\d:\d\d, .*flash \d\d:\d\d\)", out)
    assert (
        f"PocketCHIP {DIR} ({TOSHIBA.name}) now has Debian trixie (pocketchip)" in out
    )
    assert f"Backup of the original NAND: {bdir} " in out
    assert I.REBOOT in out and "`chip`, password `chip`" in out and "`passwd`" in out
    assert "Wi-Fi" not in out
    assert sorted(p.name for p in (cache / I.DEVICES).iterdir()) == [DIR]
    assert sorted(p.name for p in (cache / I.DEVICES / DIR).iterdir()) == [
        I.BACKUP,
        I.FLASH,
    ]


def test_resume_after_interrupted_backup(env, capsys):
    dev, cache, argv = env
    dev.fail = lambda a, n: "-U" in a and "raw3" in a
    assert main(argv) == 1
    assert "install stopped" in capsys.readouterr().err
    m = B.load_manifest(cache / I.DEVICES / DIR / I.BACKUP)
    assert (m["next_eraseblock"], m["complete"]) == (1, False)
    assert dev.state == "fel" and not dev.nand.erased
    dev.fail, dev.scripts = None, []
    assert main(argv) == 0
    assert backups(dev) == ["0x400000"]
    assert B.load_manifest(cache / I.DEVICES / DIR / I.BACKUP)["complete"]


def test_complete_backup_skipped(env, capsys):
    dev, _, argv = env
    assert main(argv) == 0
    dev.booted, dev.scripts = [], []
    assert main(argv + ["--verify-backup"]) == 0
    assert len(dev.booted) == 2 and not backups(dev)
    assert "2/2 eraseblocks complete" in capsys.readouterr().out


def test_no_backup(env, capsys):
    dev, cache, argv = env
    assert main(argv + ["--no-backup"]) == 0
    out = capsys.readouterr().out
    assert "skipped (--no-backup)" in out and "No backup was taken" in out
    assert len(dev.booted) == 2 and not backups(dev)
    assert not (cache / I.DEVICES / DIR / I.BACKUP).exists()


def test_devices_keep_separate_state(env):
    dev, cache, argv = env
    assert main(argv) == 0
    dev.sid = "12345678:9abcdef0:12345678:9abcdef0"
    dev.scripts = []
    assert main(argv) == 0
    assert backups(dev) == ["0x0", "0x400000"]
    dirs = sorted(p.name for p in (cache / I.DEVICES).iterdir())
    assert dirs == [DIR, "12345678-9abcdef0-12345678-9abcdef0"]
    assert all(B.load_manifest(cache / I.DEVICES / d / I.BACKUP) for d in dirs)


def test_sid_unreadable(env, capsys):
    dev, _, argv = env
    dev.sid = "SID registers for your SoC are unknown"
    assert main(argv) == 1
    assert "no SID in `sunxi-fel sid` output" in capsys.readouterr().err
    assert not dev.booted


def test_host_paths_and_restore_by_sid(env, capsys, monkeypatch):
    dev, cache, argv = env
    monkeypatch.setenv(I.HOST_CACHE, "/home/me/pocketrechip/cache")
    assert main(argv) == 0
    host = f"/home/me/pocketrechip/cache/devices/{DIR}/backup"
    assert f"Backup of the original NAND: {host} " in capsys.readouterr().out
    restore = ["restore", *argv[1:5], "--chunk-ebs", "1"]
    assert main(restore) == 0
    out = capsys.readouterr().out
    assert '"written": 2' in out and f"restore complete: {I.REBOOT}" in out
    raw = (cache / I.DEVICES / DIR / I.BACKUP / B.RAW_FILE).read_bytes()
    assert dev.nand.raw_eraseblock(0) + dev.nand.raw_eraseblock(1) == raw
    assert dev.state == "fel"


def test_connect_times_out(tmp_path, monkeypatch):
    dev = FakeDevice(None)
    dev.state = "dfu"
    monkeypatch.setattr(fel_agent, "subprocess_runner", dev)
    with pytest.raises(TimeoutError, match="no FEL device"):
        I.connect(tmp_path, 0)


def rootfs_tar(cache: Path, monkeypatch) -> R.Asset:
    """A pocketchip rootfs with /boot/boot.scr, pinned in the cache."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("./boot/boot.scr")
        info.size = len(BOOT_SCR)
        tar.addfile(info, io.BytesIO(BOOT_SCR))
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    asset = R.Asset("x-chip-os", "t", "pocketchip-rootfs.tar.gz", sha)
    asset.path(cache).parent.mkdir(parents=True, exist_ok=True)
    asset.path(cache).write_bytes(data)
    monkeypatch.setitem(R.ROOTFS, "pocketchip", asset)
    return asset


@pytest.fixture(name="private")
def private_fixture(env, monkeypatch):
    """Root faked; mkfs.ubifs packs the tree and registers the image with the board."""
    dev, cache, argv = env
    rootfs_tar(cache, monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *a, **kw: None)
    seen = {}

    def runner(cmd):
        if cmd[0] != "mkfs.ubifs":
            return dev(cmd)
        root, out = Path(cmd[cmd.index("-d") + 1]), Path(cmd[cmd.index("-o") + 1])
        files = sorted(p for p in root.rglob("*") if p.is_file())
        keyfiles = [p for p in files if p.suffix == W.SUFFIX]
        seen.update(
            out=out,
            modes={p.name: stat.S_IMODE(p.stat().st_mode) for p in keyfiles},
            dir_mode=stat.S_IMODE(out.parent.stat().st_mode),
        )
        image = b"".join(bytes(p.relative_to(root)) + p.read_bytes() for p in files)
        out.write_bytes(image)
        dev.ubi.register(image, {"/boot/boot.scr": BOOT_SCR})
        return ""

    monkeypatch.setattr(fel_agent, "subprocess_runner", runner)
    monkeypatch.setenv(W.ENV, PASSWORD)
    return (
        dev,
        cache,
        argv + ["--no-backup", "--no-overlay", "--wifi", "Home Net"],
        seen,
    )


def assert_no_secrets(cache: Path, *texts: str) -> None:
    psk = W.psk(PASSWORD, "Home Net").encode()
    for path in cache.rglob("*"):
        assert path.suffix != W.SUFFIX, path
        if path.is_file():
            data = path.read_bytes()
            assert psk not in data and PASSWORD.encode() not in data, path
    for text in texts:
        assert psk.decode() not in text and PASSWORD not in text


def test_wifi_image_is_private_and_removed(private, capsys, caplog):
    caplog.set_level("DEBUG")
    dev, cache, argv, seen = private
    assert main(argv) == 0
    out = capsys.readouterr()
    assert seen["modes"] == {"Home_Net.nmconnection": 0o600}
    assert seen["dir_mode"] == 0o700 and not seen["out"].parent.exists()
    assert cache not in seen["out"].parents
    psk = W.psk(PASSWORD, "Home Net")
    assert f"psk={psk}".encode() in bytes(dev.ubi.volume)
    assert "joins Wi-Fi 'Home Net' and runs openssh-server" in out.out
    assert "`ip a`" in out.out and "ssh chip@<address>" in out.out
    assert_no_secrets(cache, out.out, out.err, caplog.text)


def test_wifi_image_removed_on_failure(private, capsys, caplog):
    caplog.set_level("DEBUG")
    dev, cache, argv, seen = private
    dev.inject = lambda words: words[:2] == ("nand", "erase.chip")
    assert main(argv) == 1
    out = capsys.readouterr()
    assert "install stopped" in out.err and not seen["out"].parent.exists()
    assert_no_secrets(cache, out.out, out.err, caplog.text)


def test_flash_wifi_open_and_dry_run_redacted(private, capsys, monkeypatch):
    dev, cache, argv, seen = private
    monkeypatch.setattr(W.getpass, "getpass", pytest.fail)
    flash = ["flash", "--out", str(cache.parent / "out"), "--backup", "x"]
    flash += argv[1:5] + ["--no-backup", "--no-overlay"]
    assert main(flash + ["--wifi", "Cafe", "--wifi-open"]) == 0
    assert seen["modes"] == {"Cafe.nmconnection": 0o600}
    assert b"[wifi-security]" not in bytes(dev.ubi.volume)
    assert not seen["out"].parent.exists()
    capsys.readouterr()
    monkeypatch.delenv(W.ENV)
    assert main(flash + ["--wifi", "Home Net", "--dry-run", "--chip", "toshiba"]) == 0
    out = capsys.readouterr().out
    assert (
        "Wi-Fi: NetworkManager profile for SSID 'Home Net' "
        "(wpa-psk, key not shown) added when flashing"
    ) in out
    assert seen["modes"] == {} and "Toshiba" in out
    assert_no_secrets(cache, out)


def test_wifi_password_rejected_before_anything(env, capsys, monkeypatch):
    dev, cache, argv = env
    monkeypatch.setenv(W.ENV, "short")
    assert main(argv + ["--wifi", "Home Net"]) == 1
    assert "8-63 printable ASCII" in capsys.readouterr().err
    assert not dev.calls and not (cache / I.DEVICES).exists()


def test_sid_constant_matches_dir():
    assert I.device_dir(Path("c"), SID) == Path("c/devices") / DIR


def test_prepare_only_touches_no_usb(env):
    dev, cache, argv = env
    assert main(argv + ["--prepare-only", "--wifi", "Home Net"]) == 0
    assert {c[0] for c in dev.calls} <= {"sunxi-nand-image-builder"}
    assert not (cache / I.DEVICES).exists()
