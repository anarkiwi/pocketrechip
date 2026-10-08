"""systemd update stamps: NeedsUpdate unit discovery, output freshness, stamp format."""

# pylint: disable=missing-function-docstring

import logging
import os
import re
import shutil

import pytest

from pocketrechip import overlay as O
from pocketrechip import update_done as U

USR_NS = 1_790_000_000 * 10**9
LIB = "usr/lib/systemd/system"
UNITS = {
    "ldconfig.service": "ConditionNeedsUpdate=|/etc\nConditionFileNotEmpty=|!/etc/ld.so.cache",
    "systemd-journal-catalog-update.service": "ConditionNeedsUpdate=/var",
    "systemd-hwdb-update.service": "ConditionNeedsUpdate=/etc",
    "systemd-sysusers.service": "ConditionNeedsUpdate=|/etc",
    "systemd-update-done.service": "ConditionNeedsUpdate=|/etc\nConditionNeedsUpdate=|/var",
    "ssh.service": "After=network.target",
}


def unit(conditions, extra=""):
    return f"[Unit]\nDescription=x\n{conditions}\n\n[Service]\n{extra}ExecStart=/bin/true\n"


def tree(tmp_path, age_ns=0):
    """Release-like root: NeedsUpdate units, their outputs age_ns newer than /usr."""
    root = tmp_path / "root"
    (root / LIB).mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "var").mkdir()
    for name, cond in UNITS.items():
        (root / LIB / name).write_text(unit(cond))
    for out in {o for outs in U.OUTPUTS.values() for o in outs}:
        (root / out).parent.mkdir(parents=True, exist_ok=True)
        (root / out).write_text("x")
        os.utime(root / out, ns=(USR_NS + age_ns,) * 2)
    os.utime(root / "usr", ns=(USR_NS,) * 2)
    return root


def needs_update(root, directory):
    """ConditionNeedsUpdate=<directory> as systemd v257 condition.c evaluates it."""
    stamp = root / directory.lstrip("/") / ".updated"
    if not stamp.exists():
        return True
    usr, other = (root / "usr").lstat(), stamp.lstat()
    u, o = divmod(usr.st_mtime_ns, 10**9), divmod(other.st_mtime_ns, 10**9)
    if u[0] != o[0]:
        return u[0] > o[0]
    if u[1] == 0 or o[1] > 0:
        return u[1] > o[1]
    m = re.search(r"^TIMESTAMP_NSEC=(\d+)$", stamp.read_text(), re.M)
    return not m or usr.st_mtime_ns > int(m[1])


def test_discovers_needs_update_units_of_the_tree(tmp_path):
    assert U.needs_update_units(tree(tmp_path)) == {
        "ldconfig.service": ["|/etc"],
        "systemd-hwdb-update.service": ["/etc"],
        "systemd-journal-catalog-update.service": ["/var"],
        "systemd-sysusers.service": ["|/etc"],
        "systemd-update-done.service": ["|/etc", "|/var"],
    }


@pytest.mark.parametrize("age_ns", [0, 7])
def test_stamps_match_systemd_update_done(tmp_path, age_ns):
    root = tree(tmp_path, age_ns)
    assert needs_update(root, "/etc") and needs_update(root, "/var")
    assert U.stale(root) == [] and U.stamp(root, os.getuid(), os.getgid())
    for rel in U.STAMPS:
        st = (root / rel).lstat()
        assert (root / rel).read_text() == (
            "# This file was created by systemd-update-done. Its only \n"
            "# purpose is to hold a timestamp of the time this directory\n"
            "# was updated. See man:systemd-update-done.service(8).\n"
            f"TIMESTAMP_NSEC={USR_NS}\n"
        )
        assert st.st_mtime_ns == st.st_atime_ns == USR_NS
        assert st.st_mode & 0o7777 == 0o644
    assert not needs_update(root, "/etc") and not needs_update(root, "/var")
    os.utime(root / "usr", ns=(USR_NS + 1,) * 2)
    assert needs_update(root, "/etc") and needs_update(root, "/var")


def test_coarse_stamp_mtime_falls_back_to_timestamp_nsec(tmp_path):
    root = tree(tmp_path, 5)
    os.utime(root / "usr", ns=(USR_NS + 5,) * 2)
    assert U.stamp(root, os.getuid(), os.getgid())
    os.utime(root / "etc/.updated", ns=(USR_NS,) * 2)
    assert not needs_update(root, "/etc")
    os.utime(root / "usr", ns=(USR_NS + 6,) * 2)
    assert needs_update(root, "/etc")


def break_missing(root):
    (root / "etc/ld.so.cache").unlink()


def break_old(root):
    os.utime(root / "var/lib/systemd/catalog/database", ns=(USR_NS - 1,) * 2)


def break_link(root):
    (root / "etc/passwd").unlink()
    (root / "etc/passwd").symlink_to("group")


def break_unknown(root):
    (root / "etc/systemd/system/local-cache.service").write_text(
        unit("ConditionNeedsUpdate=/var")
    )


def break_hwdb_d(root):
    (root / "etc/udev/hwdb.d").mkdir(parents=True)
    (root / "etc/udev/hwdb.d/60-local.hwdb").write_text("evdev:*\n")


def break_usr(root):
    os.utime(root / "usr", ns=(USR_NS + 10**9,) * 2)


def break_var(root):
    shutil.rmtree(root / "var")


@pytest.mark.parametrize(
    "breaker,reason",
    [
        (break_missing, "ldconfig.service: /etc/ld.so.cache missing"),
        (
            break_old,
            "systemd-journal-catalog-update.service: "
            "/var/lib/systemd/catalog/database older than /usr",
        ),
        (break_link, "systemd-sysusers.service: /etc/passwd missing"),
        (break_unknown, "local-cache.service: no known output"),
        (break_hwdb_d, "systemd-hwdb-update.service: /etc/udev/hwdb.bin missing"),
        (break_usr, "ldconfig.service: /etc/ld.so.cache older than /usr"),
        (break_var, "/var is not a directory"),
    ],
)
def test_no_stamp_when_an_update_is_due(tmp_path, caplog, breaker, reason):
    root = tree(tmp_path)
    breaker(root)
    with caplog.at_level(logging.WARNING):
        assert not U.stamp(root, os.getuid(), os.getgid())
    assert reason in U.stale(root) and reason in caplog.text
    assert not any(os.path.lexists(root / s) for s in U.STAMPS)


def test_no_usr(tmp_path):
    assert U.stale(tmp_path) == ["/usr is not a directory"]


def test_hwdb_output_moves_to_etc_with_local_sources(tmp_path):
    root = tree(tmp_path)
    assert U.outputs(root, U.HWDB) == ("usr/lib/udev/hwdb.bin",)
    break_hwdb_d(root)
    (root / "etc/udev/hwdb.bin").write_text("x")
    assert U.outputs(root, U.HWDB) == ("etc/udev/hwdb.bin",)
    assert U.stale(root) == [] and U.outputs(root, "x.service") is None


def test_masks_overrides_links_and_drop_ins(tmp_path):
    root = tree(tmp_path)
    etc = root / "etc/systemd/system"
    (etc / "systemd-sysusers.service").symlink_to("/dev/null")
    (root / "opt").mkdir()
    (root / "opt/ldconfig.service").write_text(unit("ConditionNeedsUpdate=/var"))
    (etc / "ldconfig.service").symlink_to("/opt/ldconfig.service")
    (etc / "catalog-alias.service").symlink_to(
        f"../../../{LIB}/systemd-journal-catalog-update.service"
    )
    drop = root / LIB / "ssh.service.d"
    drop.mkdir()
    (drop / "50-a.conf").write_text(
        "[Unit]\nConditionNeedsUpdate=/etc\n[Service]\nConditionNeedsUpdate=/x\n"
    )
    (drop / "60-b.conf").write_text("[Unit]\nConditionNeedsUpdate=/y\n")
    (etc / "ssh.service.d").mkdir()
    (etc / "ssh.service.d/60-b.conf").write_text(
        "[Unit]\nConditionNeedsUpdate=\nConditionNeedsUpdate=/var\n"
    )
    (etc / "ssh.service.d/70-dangling.conf").symlink_to("/nowhere.conf")
    (etc / "loop.service").symlink_to("loop.service")
    (etc / "multi-user.target.wants").mkdir()
    (etc / "multi-user.target.wants/hwdb.service").symlink_to(
        f"/{LIB}/systemd-hwdb-update.service"
    )
    units = U.needs_update_units(root)
    assert set(units) == {
        "ldconfig.service",
        "catalog-alias.service",
        "ssh.service",
        "systemd-hwdb-update.service",
        "systemd-journal-catalog-update.service",
        "systemd-update-done.service",
    }
    assert units["ldconfig.service"] == units["ssh.service"] == ["/var"]
    assert units["catalog-alias.service"] == ["/var"]


def test_overlay_apply_stamps_a_current_tree(tmp_path):
    root = tree(tmp_path)
    (root / "etc/fstab").write_text("")
    O.apply(O.SEARCH[0], root, os.getuid(), os.getgid())
    assert all((root / s).is_file() for s in U.STAMPS)
    assert not needs_update(root, "/etc") and not needs_update(root, "/var")
