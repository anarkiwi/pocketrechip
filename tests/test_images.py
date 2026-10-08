"""UBIFS build: extraction, overlay, mkfs.ubifs arguments, cache key."""

# pylint: disable=missing-function-docstring

import io
import json
import os
import shutil
import subprocess
import tarfile

import pytest

from pocketrechip import images as I
from pocketrechip import overlay as O


def make_tar(path, files):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.uid, info.gid = len(data), 0, 0
            tar.addfile(info, io.BytesIO(data))
    return path


def fake_mkfs(calls):
    def runner(argv):
        calls.append(argv)
        root = argv[argv.index("-d") + 1]
        names = sorted(str(p.relative_to(root)) for p in I.Path(root).rglob("*"))
        I.Path(argv[argv.index("-o") + 1]).write_text("\n".join(names), "utf-8")
        return ""

    return runner


@pytest.fixture(name="as_root")
def as_root_fixture(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "lchown", lambda *a: None)


def test_build_ubifs_applies_overlay_and_caches(tmp_path, as_root):
    del as_root
    tar = make_tar(
        tmp_path / "r.tar.gz", {"./boot/boot.scr": b"scr", "./etc/fstab": b"x"}
    )
    calls = []
    ub = I.build_ubifs(tar, "ab" * 32, O.SEARCH[0], tmp_path / "c", fake_mkfs(calls))
    assert calls[0][:9] == [
        "mkfs.ubifs",
        "-m",
        "16384",
        "-e",
        "0x1f8000",
        "-c",
        "4096",
        "-x",
        "zlib",
    ]
    assert "etc/systemd/system/zram-swap.service" in ub.image.read_text("utf-8").split()
    assert ub.boot_scr.read_bytes() == b"scr"
    meta = json.loads(ub.image.with_suffix(".json").read_text("utf-8"))
    assert meta["overlay_sha256"] == O.digest(O.SEARCH[0]) and meta["size"] == ub.size
    assert I.cached_ubifs("ab" * 32, O.SEARCH[0], tmp_path / "c") == ub
    assert I.build_ubifs(tar, "ab" * 32, O.SEARCH[0], tmp_path / "c", None) == ub
    assert len(calls) == 1
    plain = I.build_ubifs(tar, "ab" * 32, None, tmp_path / "c", fake_mkfs(calls))
    assert plain != ub and "etc/sysctl.d" not in plain.image.read_text("utf-8")
    assert I.ubifs_key("ab" * 32, None) != I.ubifs_key("ab" * 32, O.SEARCH[0])


def test_build_ubifs_needs_boot_scr_and_root(tmp_path, as_root, monkeypatch):
    del as_root
    tar = make_tar(tmp_path / "r.tar.gz", {"./etc/fstab": b"x"})
    with pytest.raises(FileNotFoundError, match="boot/boot.scr"):
        I.build_ubifs(tar, "cd" * 32, None, tmp_path / "c", fake_mkfs([]))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError, match="needs root"):
        I.build_ubifs(tar, "cd" * 32, None, tmp_path / "c", fake_mkfs([]))


def test_extract_reports_tar_failure(tmp_path):
    bad = tmp_path / "bad.tar"
    bad.write_bytes(b"not a tar" * 100)
    with pytest.raises(subprocess.CalledProcessError):
        I.extract(bad, tmp_path)


@pytest.mark.skipif(not shutil.which("mkfs.ubifs"), reason="no mkfs.ubifs")
def test_real_mkfs_ubifs(tmp_path, as_root):
    del as_root
    tar = make_tar(tmp_path / "r.tar.gz", {"./boot/boot.scr": b"scr" * 1000})
    ub = I.build_ubifs(tar, "ef" * 32, None, tmp_path / "c")
    head = ub.image.read_bytes()[:24]
    assert head[:4] == b"\x31\x18\x10\x06" and ub.size % (16384) == 0
