"""Rootfs overlay: tree copy, removals, ownership, idempotence, digest."""

# pylint: disable=missing-function-docstring

import os
import re
import shlex
import subprocess
from pathlib import PurePosixPath

import pytest

from pocketrechip import overlay as O
from pocketrechip import qemu_smoke as Q

REPO = O.SEARCH[0]
ZRAM = "etc/systemd/system/zram-swap.service"
WANTS = (
    "etc/systemd/system/graphical.target.wants/udisks2.service",
    "etc/systemd/system/timers.target.wants/e2scrub_all.timer",
    "etc/systemd/system/timers.target.wants/fstrim.timer",
    "etc/systemd/system/multi-user.target.wants/e2scrub_reap.service",
    "etc/systemd/system/multi-user.target.wants/systemd-networkd.service",
)


def fake_root(tmp_path):
    root = tmp_path / "root"
    wants = root / "etc/systemd/system/timers.target.wants"
    wants.mkdir(parents=True)
    (root / "etc/fstab").write_text("# UNCONFIGURED FSTAB FOR BASE SYSTEM\n")
    (wants / "plocate-updatedb.timer").symlink_to("/lib/systemd/system/x.timer")
    (root / "etc/systemd/system/plocate-updatedb.timer").write_text("old")
    for link in WANTS:
        (root / link).parent.mkdir(parents=True, exist_ok=True)
        (root / link).symlink_to(f"/usr/lib/systemd/system/{PurePosixPath(link).name}")
    return root


def snapshot(root):
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        kind = (
            os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else "d"
        )
        out[str(p.relative_to(root))] = (kind, st.st_mode, st.st_uid, st.st_gid)
    return out


def test_repo_overlay_applies_and_is_idempotent(tmp_path):
    root = fake_root(tmp_path)
    ids = (os.getuid(), os.getgid())
    O.apply(REPO, root, *ids)
    snap = snapshot(root)
    assert (
        root / "etc/fstab"
    ).read_text() == "ubi0:rootfs / ubifs noatime,bulk_read 0 0\n"
    assert not os.path.lexists(
        root / "etc/systemd/system/timers.target.wants/plocate-updatedb.timer"
    )
    assert (
        os.readlink(root / "etc/systemd/system/plocate-updatedb.timer") == "/dev/null"
    )
    link = root / "etc/systemd/system/swap.target.wants/zram-swap.service"
    assert os.readlink(link) == "/" + ZRAM
    assert (
        "Storage=volatile"
        in (root / "etc/systemd/journald.conf.d/90-pocketrechip.conf").read_text()
    )
    assert (root / "etc/sysctl.d/90-pocketrechip-zram.conf").read_text() == (
        "vm.page-cluster = 0\n"
    )
    assert not any(os.path.lexists(root / l) for l in WANTS)
    masks = {
        p.name
        for p in (root / "etc/systemd/system").iterdir()
        if p.is_symlink() and os.readlink(p) == "/dev/null"
    }
    assert masks == {*Q.MASKED, "plocate-updatedb.timer"}
    assert (root / "etc/systemd/system-preset/80-pocketrechip.preset").read_text() == (
        "disable udisks2.service\n"
    )
    assert not os.path.lexists(root / "etc/systemd/system" / Q.UDISKS)
    assert snap[ZRAM][1] & 0o7777 == 0o644
    assert snap["etc/systemd/journald.conf.d"][1] & 0o7777 == 0o755
    assert {v[2:] for v in snap.values()} == {ids}
    O.apply(REPO, root, *ids)
    assert snapshot(root) == snap


def test_apply_chowns_to_root_by_default(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "lchown", lambda p, u, g: calls.append((u, g)))
    O.apply(REPO, fake_root(tmp_path))
    assert calls and set(calls) == {(0, 0)}


def test_zram_unit_commands_are_valid_shell():
    unit = (REPO / "rootfs" / ZRAM).read_text()
    execs = dict(re.findall(r"^(ExecSt\w+)=(.*)$", unit, re.M))
    assert set(execs) == {"ExecStart", "ExecStop"}
    for line in execs.values():
        argv = shlex.split(line.replace("$$", "$"))
        assert argv[:2] == ["/bin/sh", "-c"]
        subprocess.run(["sh", "-n", "-c", argv[2]], check=True)
    start = shlex.split(execs["ExecStart"].replace("$$", "$"))[2]
    assert re.search(
        r"modprobe zram .*zramctl --find --size .*mkswap .*swapon -p 100", start
    )
    assert " -a " not in start
    assert "int($2 / 2)" in start
    for key in (
        "DefaultDependencies=no",
        "Before=swap.target",
        "RemainAfterExit=yes",
        "After=systemd-modules-load.service",
        "Type=oneshot",
    ):
        assert key in unit.splitlines()


def test_digest_tracks_content_mode_links_and_removals(tmp_path):
    lay = tmp_path / "ov"
    (lay / "rootfs/etc").mkdir(parents=True)
    (lay / "rootfs/etc/a").write_text("x")
    (lay / "rootfs/l").symlink_to("/dev/null")
    seen = {O.digest(lay)}
    (lay / "rootfs/etc/a").chmod(0o755)
    seen.add(O.digest(lay))
    (lay / "rootfs/etc/a").chmod(0o775)
    assert O.digest(lay) in seen
    (lay / "rootfs/etc/a").write_text("y")
    seen.add(O.digest(lay))
    (lay / "rootfs/l").unlink()
    (lay / "rootfs/l").symlink_to("/dev/zero")
    seen.add(O.digest(lay))
    (lay / "remove").write_text("# comment\n/etc/b\n\n")
    seen.add(O.digest(lay))
    assert len(seen) == 5 and O.digest(None) == "none"
    assert O.removals(lay) == [O.Path("etc/b")]


def test_refuses_escapes(tmp_path):
    lay = tmp_path / "ov"
    (lay / "rootfs/etc").mkdir(parents=True)
    (lay / "rootfs/etc/a").write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    (root / "etc").symlink_to(tmp_path / "host")
    with pytest.raises(ValueError, match="etc is a symlink"):
        O.apply(lay, root, os.getuid(), os.getgid())
    (lay / "remove").write_text("etc/x\n")
    with pytest.raises(ValueError, match="parent etc is a symlink"):
        O.apply(lay, root, os.getuid(), os.getgid())
    (lay / "remove").write_text("../x\n")
    with pytest.raises(ValueError, match="bad overlay removal"):
        O.removals(lay)


def test_replaces_dir_with_file_and_file_with_dir(tmp_path):
    lay = tmp_path / "ov"
    (lay / "rootfs/d").mkdir(parents=True)
    (lay / "rootfs/f").write_text("new")
    root = tmp_path / "root"
    (root / "f").mkdir(parents=True)
    (root / "d").write_text("old")
    O.apply(lay, root, os.getuid(), os.getgid())
    assert (root / "d").is_dir() and (root / "f").read_text() == "new"


def test_default_overlay(monkeypatch, tmp_path):
    monkeypatch.setenv("POCKETRECHIP_OVERLAY", str(tmp_path))
    assert O.default() == tmp_path
    monkeypatch.delenv("POCKETRECHIP_OVERLAY")
    assert O.default() == REPO
    monkeypatch.setattr(O, "SEARCH", (tmp_path,))
    assert O.default() is None
