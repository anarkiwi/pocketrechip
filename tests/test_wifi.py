"""Wi-Fi profile: PSK derivation, validation, keyfile format, password sources."""

# pylint: disable=missing-function-docstring

import os
import shutil
import stat
import subprocess
import uuid

import pytest

from pocketrechip import wifi as W

PSK = "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"


@pytest.mark.parametrize(
    "password,ssid,want",
    [
        ("password", "IEEE", PSK),
        (
            "ThisIsAPassword",
            "ThisIsASSID",
            "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af",
        ),
        (
            "a" * 32,
            "Z" * 32,
            "becb93866bb8c3832cb777c2f559807c8c59afcb6eae734885001300a981cc62",
        ),
        (PSK.upper(), "any", PSK),
    ],
)
def test_psk_ieee_80211i_vectors(password, ssid, want):
    assert W.psk(password, ssid) == want


@pytest.mark.parametrize(
    "password,ok",
    [
        ("1234567", False),
        ("12345678", True),
        ("x" * 63, True),
        ("x" * 64, False),
        (PSK, True),
        (PSK[:-1] + "g", False),
        ("pass word ~!", True),
        ("passwörd1", False),
        ("pass\tword", False),
    ],
)
def test_check_password(password, ok):
    if ok:
        assert W.check_password(password) == password
    else:
        with pytest.raises(ValueError, match="8-63 printable ASCII"):
            W.check_password(password)


@pytest.mark.parametrize("ssid", ["", "x" * 33, "é" * 17, "a\nb", "a\x7f"])
def test_bad_ssid(ssid):
    with pytest.raises(ValueError, match="SSID must be"):
        W.Profile(ssid)


def test_keyfile_exact():
    prof = W.Profile("Home Net", PSK)
    assert prof.keyfile() == (
        "[connection]\nid=Home Net\n"
        f"uuid={prof.uuid}\ntype=wifi\nautoconnect=true\n\n"
        "[wifi]\nmode=infrastructure\nssid=Home Net\n\n"
        f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={PSK}\n\n"
        "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"
    )
    assert PSK not in repr(prof) and "Home Net" in repr(prof)
    assert prof.filename == "Home_Net.nmconnection"


def test_open_keyfile_has_no_security():
    prof = W.Profile("Cafe")
    assert "[wifi-security]" not in prof.keyfile() and "psk" not in prof.keyfile()
    assert prof.describe() == "Wi-Fi: NetworkManager profile for SSID 'Cafe' (open)"
    assert "key not shown" in W.Profile("Cafe", PSK).describe()


def test_uuid_deterministic_per_ssid():
    assert W.Profile("a").uuid == W.Profile("a", PSK).uuid != W.Profile("b").uuid
    assert uuid.UUID(W.Profile("a").uuid).version == 5


@pytest.mark.parametrize(
    "ssid,want",
    [
        ("plain-SSID 1", "plain-SSID 1"),
        ("12;34;", "49;50;59;51;52;59;"),
        ("a\\b", "97;92;98;"),
        (" lead", "32;108;101;97;100;"),
        ("trail ", "116;114;97;105;108;32;"),
        ("café", "99;97;102;195;169;"),
    ],
)
def test_ssid_value(ssid, want):
    assert W.kf_ssid(ssid) == want


def test_id_escaping():
    assert W.kf_string("  a\\b ;# ") == "\\s\\sa\\\\b ;#\\s"
    assert W.kf_string("plain") == "plain"
    assert W.Profile("café;x").filename == "caf__x.nmconnection"


def test_apply_writes_root_0600(tmp_path, monkeypatch):
    owners = []
    monkeypatch.setattr(os, "chown", lambda p, u, g, **kw: owners.append((p, u, g)))
    path = W.Profile("Net", PSK).apply(tmp_path)
    assert path == tmp_path / W.DIR / "Net.nmconnection"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert owners == [(path, 0, 0)] and f"psk={PSK}" in path.read_text()
    path.chmod(0o644)
    W.Profile("Net").apply(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "psk" not in path.read_text()
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        W.Profile("Net", PSK).apply(tmp_path)
    assert not (tmp_path / "elsewhere").exists()


def test_password_sources(tmp_path, monkeypatch):
    monkeypatch.delenv(W.ENV, raising=False)
    answers = iter(["password", "password", "password", "other", "short", "short"])
    monkeypatch.setattr(W.getpass, "getpass", lambda prompt: next(answers))
    assert W.read_password("IEEE") == "password"
    with pytest.raises(ValueError, match="do not match"):
        W.read_password("IEEE")
    with pytest.raises(ValueError, match="8-63"):
        W.read_password("IEEE")
    monkeypatch.setenv(W.ENV, "from the env")
    assert W.read_password("x") == "from the env"
    pw = tmp_path / "pw"
    pw.write_text("from a file\r\n")
    assert W.read_password("x", pw) == "from a file"
    assert W.profile("IEEE", password_file=pw) == W.Profile(
        "IEEE", W.psk("from a file", "IEEE")
    )


def test_profile_options(tmp_path):
    assert W.profile(None) is None
    assert W.profile("Cafe", is_open=True) == W.Profile("Cafe")
    with pytest.raises(ValueError, match="need --wifi"):
        W.profile(None, is_open=True)
    with pytest.raises(ValueError, match="takes no password"):
        W.profile("Cafe", True, tmp_path / "pw")
    with pytest.raises(ValueError, match="SSID"):
        W.profile("x" * 40, True)


@pytest.mark.skipif(not shutil.which("nmcli"), reason="no nmcli")
@pytest.mark.parametrize("ssid", ["Home Net", "12;34;", "a\\b", " lead", "café"])
def test_networkmanager_reads_keyfile(ssid):
    prof = W.Profile(ssid, PSK)
    out = subprocess.run(
        ["nmcli", "--offline", "connection", "modify", "connection.autoconnect", "yes"],
        input=prof.keyfile(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = out.splitlines()
    assert f"id={W.kf_string(ssid)}" in lines and f"psk={PSK}" in lines
    assert f"uuid={prof.uuid}" in lines
