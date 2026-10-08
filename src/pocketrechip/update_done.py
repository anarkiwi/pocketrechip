"""systemd-update-done's /etc/.updated and /var/.updated for a tree whose caches are current.

Format and comparison follow systemd v257 src/update-done/update-done.c and
src/shared/condition.c (ConditionNeedsUpdate=); see docs/flash.md "Rootfs overlay".
"""

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

MESSAGE = (
    "# This file was created by systemd-update-done. Its only \n"
    "# purpose is to hold a timestamp of the time this directory\n"
    "# was updated. See man:systemd-update-done.service(8).\n"
)
STAMPS = ("etc/.updated", "var/.updated")
UNIT_DIRS = (
    "etc/systemd/system",
    "usr/local/lib/systemd/system",
    "usr/lib/systemd/system",
)
HWDB = "systemd-hwdb-update.service"
OUTPUTS = {
    "ldconfig.service": ("etc/ld.so.cache",),
    "systemd-journal-catalog-update.service": ("var/lib/systemd/catalog/database",),
    HWDB: ("usr/lib/udev/hwdb.bin",),
    "systemd-sysusers.service": (
        "etc/passwd",
        "etc/group",
        "etc/shadow",
        "etc/gshadow",
    ),
    "systemd-update-done.service": (),
}
CONDITION = re.compile(r"^\s*ConditionNeedsUpdate\s*=\s*(.*?)\s*$")
SECTION = re.compile(r"^\s*\[(.*)\]\s*$")
MAX_LINKS = 32


def _follow(root: Path, path: Path) -> Path | None:
    """path with symlinks resolved inside root; None for a mask (/dev/null) or a loop."""
    for _ in range(MAX_LINKS):
        if not path.is_symlink():
            return path
        target = os.readlink(path)
        if target == "/dev/null":
            return None
        path = (
            (root / target.lstrip("/")) if target[:1] == "/" else path.parent / target
        )
    return None


def _sources(root: Path, name: str) -> list[Path] | None:
    """The unit file and its drop-ins in load order; None if masked or absent."""
    main = next(
        (root / d / name for d in UNIT_DIRS if os.path.lexists(root / d / name)), None
    )
    main = main and _follow(root, main)
    if main is None or not main.is_file():
        return None
    drop: dict[str, Path] = {}
    for d in reversed(UNIT_DIRS):
        drop |= {p.name: p for p in (root / d / f"{name}.d").glob("*.conf")}
    found = (_follow(root, drop[k]) for k in sorted(drop))
    return [main, *(p for p in found if p and p.is_file())]


def conditions(sources: list[Path]) -> list[str]:
    """ConditionNeedsUpdate= values of [Unit] sections in order; an empty one resets."""
    out: list[str] = []
    for src in sources:
        section = ""
        for line in src.read_text(errors="replace").splitlines():
            if m := SECTION.match(line):
                section = m[1]
            elif section == "Unit" and (m := CONDITION.match(line)):
                out = [*out, m[1]] if m[1] else []
    return out


def needs_update_units(root: Path) -> dict[str, list[str]]:
    """Unmasked units of the tree with ConditionNeedsUpdate=, and its values."""
    names = {
        p.name
        for d in UNIT_DIRS
        if (root / d).is_dir()
        for p in (root / d).iterdir()
        if not p.name.endswith((".d", ".wants", ".requires"))
    }
    found = {name: _sources(root, name) for name in sorted(names)}
    return {n: c for n, src in found.items() if src and (c := conditions(src))}


def outputs(root: Path, unit: str) -> tuple[str, ...] | None:
    """Files a NeedsUpdate unit produces, None if unknown; systemd-hwdb writes
    /etc/udev/hwdb.bin instead when /etc/udev/hwdb.d has sources."""
    if unit == HWDB and any((root / "etc/udev/hwdb.d").glob("*.hwdb")):
        return ("etc/udev/hwdb.bin",)
    return OUTPUTS.get(unit)


def stale(root: Path) -> list[str]:
    """Why the tree would still need its first-boot updates; empty when it would not."""
    usr = root / "usr"
    if usr.is_symlink() or not usr.is_dir():
        return ["/usr is not a directory"]
    why = [
        f"/{d} is not a directory" for d in ("etc", "var") if not (root / d).is_dir()
    ]
    since = usr.lstat().st_mtime_ns
    for unit in needs_update_units(root):
        out = outputs(root, unit)
        if out is None:
            why.append(f"{unit}: no known output")
        for rel in out or ():
            path = root / rel
            if path.is_symlink() or not path.is_file():
                why.append(f"{unit}: /{rel} missing")
            elif path.stat().st_mtime_ns < since:
                why.append(f"{unit}: /{rel} older than /usr")
    return why


def stamp(root: Path, uid: int = 0, gid: int = 0) -> bool:
    """Write both stamps as systemd-update-done does, if no first-boot update is due."""
    if why := stale(root):
        log.warning("no update stamps: %s", "; ".join(why))
        return False
    ns = (root / "usr").lstat().st_mtime_ns
    for rel in STAMPS:
        path = root / rel
        path.unlink(missing_ok=True)
        path.write_text(f"{MESSAGE}TIMESTAMP_NSEC={ns}\n")
        os.chmod(path, 0o644)
        os.lchown(path, uid, gid)
        os.utime(path, ns=(ns, ns))
    log.info("update stamps at the /usr mtime, %d ns", ns)
    return True
