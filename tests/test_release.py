"""Pinned release assets: download, verification, cache reuse."""

# pylint: disable=missing-function-docstring

import hashlib

import pytest

from pocketrechip import release as R

CACHE = R.Path(__file__).resolve().parents[1] / "cache"


def local_asset(tmp_path, monkeypatch, data, sha=None):
    src = tmp_path / "src" / "repo" / "tag"
    src.mkdir(parents=True)
    (src / "a.bin").write_bytes(data)
    monkeypatch.setattr(R, "URL", f"file://{tmp_path}/src/{{repo}}/{{tag}}/{{name}}")
    return R.Asset("repo", "tag", "a.bin", sha or hashlib.sha256(data).hexdigest())


def test_fetch_downloads_verifies_and_reuses(tmp_path, monkeypatch):
    asset = local_asset(tmp_path, monkeypatch, b"payload")
    path = R.fetch(asset, tmp_path / "c")
    assert path == tmp_path / "c/releases/tag/a.bin" and path.read_bytes() == b"payload"
    (tmp_path / "src/repo/tag/a.bin").unlink()
    assert R.fetch(asset, tmp_path / "c").read_bytes() == b"payload"


def test_fetch_replaces_corrupt_cache(tmp_path, monkeypatch):
    asset = local_asset(tmp_path, monkeypatch, b"payload")
    dest = asset.path(tmp_path / "c")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"corrupt")
    assert R.fetch(asset, tmp_path / "c").read_bytes() == b"payload"


def test_fetch_refuses_bad_sha(tmp_path, monkeypatch):
    asset = local_asset(tmp_path, monkeypatch, b"payload", "00" * 32)
    with pytest.raises(R.ChecksumError, match="pinned 0000"):
        R.fetch(asset, tmp_path / "c")
    assert not list((tmp_path / "c/releases/tag").iterdir())


def test_pinned_table():
    assert R.UBOOT_FEL.url == (
        "https://github.com/anarkiwi/x-chip-uboot/releases/download/"
        "uboot-2026.10.08-231804/u-boot-sunxi-with-spl.bin"
    )
    assert sorted(R.ROOTFS) == ["gui", "headless", "pocketchip"]
    assert {a.tag for a in R.ROOTFS.values()} == {R.OS_TAG}


@pytest.mark.parametrize(
    "asset", [R.SPL, R.UBOOT_DTB, R.UBOOT_FEL, R.ROOTFS["pocketchip"]]
)
def test_cached_downloads_match_pins(asset):
    path = asset.path(CACHE)
    if not path.exists():
        pytest.skip(f"{path} not downloaded")
    assert R.sha256_file(path) == asset.sha256
