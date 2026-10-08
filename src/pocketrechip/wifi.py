"""Wi-Fi profile baked into the image: a NetworkManager keyfile holding only the WPA PSK.

Format per nm-settings-keyfile(5) and libnm-core nm-keyfile.c (`ssid_writer`, `get_bytes`).
NetworkManager ignores keyfiles not owned by root or readable by group or others.
"""

import getpass
import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ENV = "POCKETRECHIP_WIFI_PASSWORD"
DIR = Path("etc/NetworkManager/system-connections")
SUFFIX = ".nmconnection"
PBKDF2_ITERATIONS = 4096
PSK_BYTES = 32
SSID_BYTES = 32
UUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/anarkiwi/pocketrechip")
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
PLAIN_SSID = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")


def check_ssid(ssid: str) -> str:
    """ssid if it is 1-32 UTF-8 bytes without control characters."""
    n = len(ssid.encode())
    if not 0 < n <= SSID_BYTES or any(ord(c) < 0x20 or ord(c) == 0x7F for c in ssid):
        raise ValueError(
            f"SSID must be 1-{SSID_BYTES} bytes without control characters"
        )
    return ssid


def check_password(password: str) -> str:
    """password if valid for WPA2-PSK: 8-63 printable ASCII, or 64 hex digits."""
    if HEX64.fullmatch(password) or (
        8 <= len(password) <= 63 and all(" " <= c <= "~" for c in password)
    ):
        return password
    raise ValueError(
        "Wi-Fi password must be 8-63 printable ASCII characters or 64 hex digits"
    )


def psk(password: str, ssid: str) -> str:
    """64-hex WPA PSK as wpa_passphrase derives it; a 64-hex password is the PSK."""
    if HEX64.fullmatch(check_password(password)):
        return password.lower()
    key = hashlib.pbkdf2_hmac(
        "sha1", password.encode(), ssid.encode(), PBKDF2_ITERATIONS, PSK_BYTES
    )
    return key.hex()


def describe(ssid: str, is_open: bool) -> str:
    """Redacted one-line description of a profile."""
    sec = "open" if is_open else "wpa-psk, key not shown"
    return f"Wi-Fi: NetworkManager profile for SSID {check_ssid(ssid)!r} ({sec})"


def kf_string(value: str) -> str:
    """GKeyFile string value: backslashes doubled, edge spaces as \\s."""
    s = value.replace("\\", "\\\\")
    lead = len(s) - len(s.lstrip(" "))
    s = "\\s" * lead + s[lead:]
    trail = len(s) - len(s.rstrip(" "))
    return s[: len(s) - trail] + "\\s" * trail


def kf_ssid(ssid: str) -> str:
    """`ssid=` value: plain printable ASCII without `;` or `\\`, else the byte list."""
    if PLAIN_SSID.fullmatch(ssid) and not set(ssid) & {";", "\\"}:
        return ssid
    return "".join(f"{b};" for b in ssid.encode())


@dataclass(frozen=True)
class Profile:
    """SSID and PSK (None for an open network); the PSK never appears in repr."""

    ssid: str
    psk: str | None = field(default=None, repr=False)

    def __post_init__(self):
        check_ssid(self.ssid)

    @property
    def uuid(self) -> str:
        """Connection UUID, deterministic per SSID."""
        return str(uuid.uuid5(UUID_NS, self.ssid))

    @property
    def filename(self) -> str:
        """Keyfile name: the SSID with characters outside [A-Za-z0-9_-] as `_`."""
        return re.sub(r"[^A-Za-z0-9_-]", "_", self.ssid) + SUFFIX

    @property
    def security(self) -> str:
        """Key management in the keyfile."""
        return "wpa-psk" if self.psk else "open"

    def describe(self) -> str:
        """Redacted one-line description."""
        return describe(self.ssid, not self.psk)

    def keyfile(self) -> str:
        """NetworkManager keyfile text."""
        sec = (
            f"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk={self.psk}\n" if self.psk else ""
        )
        return (
            f"[connection]\nid={kf_string(self.ssid)}\nuuid={self.uuid}\n"
            f"type=wifi\nautoconnect=true\n\n"
            f"[wifi]\nmode=infrastructure\nssid={kf_ssid(self.ssid)}\n{sec}\n"
            "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"
        )

    def apply(self, root: Path, uid: int = 0, gid: int = 0) -> Path:
        """Write the keyfile into root, mode 0600 owned by uid:gid."""
        path = Path(root) / DIR / self.filename
        path.parent.mkdir(0o755, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(fd, 0o600)
            f.write(self.keyfile())
        os.chown(path, uid, gid, follow_symlinks=False)
        return path


def read_password(ssid: str, path: Path | None = None) -> str:
    """Password from path, else $POCKETRECHIP_WIFI_PASSWORD, else a no-echo prompt."""
    if path is not None:
        return check_password(Path(path).read_text(encoding="utf-8").rstrip("\r\n"))
    if env := os.environ.get(ENV):
        return check_password(env)
    first = check_password(getpass.getpass(f"Wi-Fi password for {ssid!r}: "))
    if getpass.getpass("Again: ") != first:
        raise ValueError("the Wi-Fi passwords do not match")
    return first


def profile(
    ssid: str | None, is_open: bool = False, password_file: Path | None = None
) -> Profile | None:
    """Profile for the command line options, reading the password; None without SSID."""
    if ssid is None:
        if is_open or password_file:
            raise ValueError("--wifi-open and --wifi-password-file need --wifi SSID")
        return None
    check_ssid(ssid)
    if is_open:
        if password_file:
            raise ValueError("--wifi-open takes no password")
        return Profile(ssid)
    return Profile(ssid, psk(read_password(ssid, password_file), ssid))
