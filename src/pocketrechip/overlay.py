"""Rootfs overlay: a tree of files and symlinks copied over the root plus paths to remove.

File modes are normalised as git stores them (0755 if any execute bit, else 0644) so the
digest and the result do not depend on the checkout's umask.
"""

import hashlib
import os
import shutil
from pathlib import Path

TREE, REMOVE = "rootfs", "remove"
SEARCH = (
    Path(__file__).resolve().parents[2] / "overlay",
    Path("/opt/pocketrechip/overlay"),
)


def default() -> Path | None:
    """POCKETRECHIP_OVERLAY, else the repo's overlay, else the Docker image's."""
    env = os.environ.get("POCKETRECHIP_OVERLAY")
    if env:
        return Path(env)
    return next((p for p in SEARCH if (p / TREE).is_dir()), None)


def _mode(path: Path) -> int:
    return 0o755 if path.lstat().st_mode & 0o111 else 0o644


def _rel(line: str) -> Path:
    rel = Path(line.strip().lstrip("/"))
    if ".." in rel.parts or not rel.parts:
        raise ValueError(f"bad overlay removal {line!r}")
    return rel


def entries(overlay: Path) -> list[tuple[Path, Path]]:
    """(relative path, source) of every file, symlink and directory in the tree, sorted."""
    tree = overlay / TREE
    return sorted((p.relative_to(tree), p) for p in tree.rglob("*"))


def removals(overlay: Path) -> list[Path]:
    """Relative paths listed in the remove file."""
    path = overlay / REMOVE
    lines = path.read_text().splitlines() if path.exists() else []
    return [_rel(l) for l in lines if l.strip() and not l.lstrip().startswith("#")]


def digest(overlay: Path | None) -> str:
    """sha256 over removals, paths, types, modes, contents and link targets."""
    if overlay is None:
        return "none"
    h = hashlib.sha256()
    for rel in removals(overlay):
        h.update(b"rm\0" + bytes(rel) + b"\0")
    for rel, src in entries(overlay):
        if src.is_symlink():
            h.update(b"ln\0" + bytes(rel) + b"\0" + os.readlink(src).encode() + b"\0")
        elif src.is_dir():
            h.update(b"dir\0" + bytes(rel) + b"\0")
        else:
            data = src.read_bytes()
            h.update(b"f\0%s\0%o\0%d\0" % (bytes(rel), _mode(src), len(data)) + data)
    return h.hexdigest()


def _inside(root: Path, rel: Path) -> Path:
    """root/rel, refusing a symlinked parent that could lead outside root."""
    for i in range(1, len(rel.parts)):
        if (root / Path(*rel.parts[:i])).is_symlink():
            raise ValueError(
                f"{root / rel}: parent {Path(*rel.parts[:i])} is a symlink"
            )
    return root / rel


def _clear(dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)


def apply(overlay: Path, root: Path, uid: int = 0, gid: int = 0) -> None:
    """Remove the listed paths, then copy the tree over root owned by uid:gid."""
    for rel in removals(overlay):
        _clear(_inside(root, rel))
    for rel, src in entries(overlay):
        dst = _inside(root, rel)
        if src.is_dir() and not src.is_symlink():
            if dst.is_symlink():
                raise ValueError(f"{dst} is a symlink, not a directory")
            if dst.is_dir():
                continue
            _clear(dst)
            dst.mkdir(0o755)
            os.chmod(dst, 0o755)
        else:
            _clear(dst)
            if src.is_symlink():
                os.symlink(os.readlink(src), dst)
            else:
                shutil.copyfile(src, dst)
                os.chmod(dst, _mode(src))
        os.lchown(dst, uid, gid)
