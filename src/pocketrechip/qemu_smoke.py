"""Boot the flashed root filesystem under full-system QEMU and check the overlay's effects.

The tree is the one `flash` feeds to mkfs.ubifs, served as the root by virtiofsd because
the release initrd has no block driver for any QEMU disk (see docs/flash.md).
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import images as I
from . import overlay as O
from . import release as R
from . import wifi as W
from .progress import progress_bar

log = logging.getLogger(__name__)

TAG = "rootfs"
MEMORY_MIB = 512
QEMU = "qemu-system-arm"
VIRTIOFSD = "/usr/libexec/virtiofsd"
APPEND = f"root={TAG} rootfstype=virtiofs rw console=ttyAMA0 panic=-1"
UNIT = "pocketrechip-smoke.service"
SCRIPT = "usr/local/sbin/pocketrechip-smoke"
MARK = "@pocketrechip-smoke"
END = "end"
QUIET = "dmesg -n 1"
UBIFS_OPTIONS = frozenset(
    (
        "fast_unmount",
        "norm_unmount",
        "bulk_read",
        "no_bulk_read",
        "chk_data_crc",
        "no_chk_data_crc",
        "compr",
        "auth_key",
        "auth_hash_name",
        "ubi",
        "vol",
        "assert",
    )
)
JOURNAL_FILES = "find {} -type f -name '*.journal*' 2>/dev/null | wc -l"
ONE_LINE = ' | awk \'BEGIN { RS = ""; FS = "\\n" } { $1 = $1; print }\''
MASKED = (
    "systemd-networkd.service",
    "systemd-networkd.socket",
    "systemd-networkd-wait-online.service",
    "systemd-network-generator.service",
    "e2scrub_all.timer",
    "e2scrub_reap.service",
    "fstrim.timer",
)
UPDATES = (
    "ldconfig.service",
    "systemd-hwdb-update.service",
    "systemd-journal-catalog-update.service",
)
UDISKS = "udisks2.service"
NM = "NetworkManager.service"
BOOT_UNITS = (*MASKED, *UPDATES, UDISKS, NM)
BOOT_PROPS = "Id,LoadState,ActiveState,ConditionResult,ConditionTimestampMonotonic"
UDISKS_CALL = (
    "busctl --timeout=600 call org.freedesktop.UDisks2 /org/freedesktop/UDisks2/Manager"
    " org.freedesktop.DBus.Properties Get ss org.freedesktop.UDisks2.Manager Version"
)
UDISKS_WAIT = (
    f"timeout 900 sh -c 'until systemctl is-active -q {UDISKS}; do sleep 2; done'"
)
BLAME_LINES = 15
PROBES = {
    "state": "systemctl is-system-running --wait",
    "swap": "swapon --show=NAME,TYPE,SIZE --noheadings --bytes",
    "root_options": "findmnt -no OPTIONS /",
    "journal_var": JOURNAL_FILES.format("/var/log/journal"),
    "journal_run": JOURNAL_FILES.format("/run/log/journal"),
    "plocate_timer": "systemctl is-enabled plocate-updatedb.timer",
    "zram_service": "systemctl is-active zram-swap.service",
    "page_cluster": "sysctl -n vm.page-cluster",
    "page_size": "getconf PAGESIZE",
    "boot_units": f"systemctl show -p {BOOT_PROPS} {' '.join(BOOT_UNITS)}{ONE_LINE}",
    "units": f"systemctl show -p Id,ActiveState,Result,NRestarts '*'{ONE_LINE}",
    "nm_keyfiles": f"stat -c '%a %U %n' /{W.DIR}/*{W.SUFFIX}",
    "nm_connections": "nmcli -t -f NAME connection show",
    "analyze": "systemd-analyze",
    "blame": f"systemd-analyze blame | head -n {BLAME_LINES}",
    "free": "free -k",
    "udisks_call": f"{UDISKS_CALL} || {{ {UDISKS_WAIT}; {UDISKS_CALL}; }}",
    "udisks_after": f"systemctl is-active {UDISKS}",
}
EXPECTED_FAILURES = {
    "ubihealthd.service": "watches /dev/ubi0, the NAND UBI device; QEMU has no NAND "
    "and the root is virtiofs",
}
SOCKET_WAIT = 30.0
POLL = 0.1


def unit_file() -> str:
    """Test-only unit running the probes after multi-user.target."""
    return (
        "[Unit]\nDescription=pocketrechip QEMU smoke test\nAfter=multi-user.target\n\n"
        f"[Service]\nType=exec\nExecStart=/bin/sh /{SCRIPT}\n"
        "StandardOutput=file:/dev/console\nStandardError=inherit\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def script() -> str:
    """Shell script printing each probe's lines as `MARK name line`, then powering off.

    QUIET keeps kernel messages from splitting probe lines on the shared console.
    """
    lines = [
        f"{{ {cmd}; }} 2>&1 | sed 's/^/{MARK} {name} /'" for name, cmd in PROBES.items()
    ]
    tail = [f"echo '{MARK} {END}'", "systemctl poweroff"]
    return "\n".join(["#!/bin/sh", QUIET, *lines, *tail]) + "\n"


def vfs_options(options: str) -> str:
    """Mount options without UBIFS's own (Linux fs/ubifs/super.c `tokens`)."""
    kept = [o for o in options.split(",") if o.split("=", 1)[0] not in UBIFS_OPTIONS]
    return ",".join(kept) or "defaults"


def fstab(text: str) -> str:
    """fstab with the `/` entry moved to the virtiofs tag, its VFS options kept."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith("#") and f[1] == "/":
            line = f"{TAG} / virtiofs {vfs_options(f[3])} 0 0"
        out.append(line + "\n")
    return "".join(out)


def install(root: Path) -> None:
    """Point the tree's fstab at virtiofs and enable the smoke unit (test copy only)."""
    tab = root / "etc/fstab"
    tab.write_text(fstab(tab.read_text()))
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    (wants.parent / UNIT).write_text(unit_file())
    (wants / UNIT).unlink(missing_ok=True)
    (wants / UNIT).symlink_to(f"/etc/systemd/system/{UNIT}")
    sh = root / SCRIPT
    sh.parent.mkdir(parents=True, exist_ok=True)
    sh.write_text(script())
    sh.chmod(0o755)


def in_tree(root: Path, name: str) -> Path:
    """root/name with a symlink (/vmlinuz, /initrd.img) resolved inside root."""
    path = root / name
    if path.is_symlink():
        target = Path(os.readlink(path))
        path = root / (target.relative_to("/") if target.is_absolute() else target)
    if ".." in path.relative_to(root).parts or not path.is_file():
        raise FileNotFoundError(f"{root / name} is not a file in the tree")
    return path


def qemu_argv(
    kernel: Path, initrd: Path, sock: Path, mem_mib: int = MEMORY_MIB
) -> list[str]:
    """Single-core 32-bit ARM virt machine, no LPAE (highmem off), root on virtiofs."""
    return [
        *(QEMU, "-M", "virt,highmem=off,memory-backend=mem", "-cpu", "cortex-a15"),
        *("-m", f"{mem_mib}M", "-smp", "1", "-nographic", "-no-reboot"),
        *("-object", f"memory-backend-memfd,id=mem,size={mem_mib}M,share=on"),
        *("-kernel", str(kernel), "-initrd", str(initrd), "-append", APPEND),
        *("-chardev", f"socket,id=fs,path={sock}"),
        *("-device", f"vhost-user-fs-pci,chardev=fs,tag={TAG}", "-nic", "none"),
    ]


def virtiofsd_argv(root: Path, sock: Path) -> list[str]:
    """virtiofsd serving root; the chroot sandbox needs no new namespaces."""
    return [VIRTIOFSD, "--shared-dir", str(root), "--socket-path", str(sock)] + [
        "--sandbox",
        "chroot",
    ]


def parse(text: str) -> dict[str, list[str]]:
    """Probe output lines by probe name, from a serial log."""
    out: dict[str, list[str]] = {}
    for m in re.finditer(rf"{re.escape(MARK)} (\w+)(?: (.*?))?\r?$", text, re.M):
        out.setdefault(m[1], []).append(m[2] or "")
    return out


def _one(sections: dict[str, list[str]], name: str) -> str:
    lines = [l.strip() for l in sections.get(name, []) if l.strip()]
    if len(lines) != 1:
        raise ValueError(f"probe {name}: expected one line, got {lines}")
    return lines[0]


def properties(units: list[str]) -> dict[str, dict[str, str]]:
    """`systemctl show` records, one per line, by Id."""
    out = {}
    for line in units:
        p = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
        if "Id" in p:
            out[p["Id"]] = p
    return out


def failing(units: list[str]) -> tuple[str, ...]:
    """Units failed, with a non-success result, or restarted after failing."""
    return tuple(
        sorted(
            u
            for u, p in properties(units).items()
            if p.get("ActiveState") == "failed"
            or p.get("Result", "success") != "success"
            or int(p.get("NRestarts") or 0)
        )
    )


@dataclass(frozen=True)
class Result:
    """Probe results of one boot."""

    state: str
    swaps: tuple[tuple[str, str, int], ...]
    root_options: tuple[str, ...]
    journal_var: int
    journal_run: int
    plocate_timer: str
    zram_service: str
    page_cluster: int
    page_size: int
    mem_total_kib: int
    failing: tuple[str, ...]
    analyze: str
    boot_units: dict[str, dict[str, str]] = field(default_factory=dict)
    blame: tuple[str, ...] = ()
    udisks_call: tuple[str, ...] = ()
    udisks_after: str = ""
    nm_keyfiles: tuple[str, ...] = ()
    nm_connections: tuple[str, ...] = ()

    @classmethod
    def from_log(cls, text: str) -> "Result":
        """Parse a serial log; ValueError if the smoke unit did not finish."""
        s = parse(text)
        if END not in s:
            raise ValueError(
                "serial log has no end marker: the smoke unit did not finish"
            )
        swaps = tuple(
            (n, t, int(b))
            for n, t, b in (l.split() for l in s.get("swap", []) if l.strip())
        )
        mem = [l.split() for l in s.get("free", []) if l.startswith("Mem:")]
        if len(mem) != 1:
            raise ValueError(f"probe free: no single Mem: line in {s.get('free')}")
        return cls(
            state=_one(s, "state"),
            swaps=swaps,
            root_options=tuple(_one(s, "root_options").split(",")),
            journal_var=int(_one(s, "journal_var")),
            journal_run=int(_one(s, "journal_run")),
            plocate_timer=_one(s, "plocate_timer"),
            zram_service=_one(s, "zram_service"),
            page_cluster=int(_one(s, "page_cluster")),
            page_size=int(_one(s, "page_size")),
            mem_total_kib=int(mem[0][1]),
            failing=failing(s.get("units", [])),
            analyze="\n".join(s.get("analyze", [])),
            boot_units=properties(s.get("boot_units", [])),
            blame=tuple(l.strip() for l in s.get("blame", []) if l.strip()),
            udisks_call=tuple(l.strip() for l in s.get("udisks_call", []) if l.strip()),
            udisks_after=_one(s, "udisks_after"),
            nm_keyfiles=tuple(l.strip() for l in s.get("nm_keyfiles", [])),
            nm_connections=tuple(
                re.sub(r"\\(.)", r"\1", l.strip()) for l in s.get("nm_connections", [])
            ),
        )


def zram_swap_bytes(mem_total_kib: int, page_size: int) -> int:
    """swapon size of zram-swap's device: half of MemTotal in whole KiB, page-aligned
    up by zram's disksize, less the header page mkswap reserves."""
    disk = -(-(mem_total_kib // 2 * 1024) // page_size) * page_size
    return disk - page_size


@dataclass(frozen=True)
class Check:
    """One expectation: observed value and whether it is as expected."""

    name: str
    expected: str
    observed: str
    ok: bool


def _is_zram_swap(swaps: tuple[tuple[str, str, int], ...], size: int) -> bool:
    return (
        len(swaps) == 1
        and re.fullmatch(r"/dev/zram\d+", swaps[0][0]) is not None
        and swaps[0][1:] == ("partition", size)
    )


def wifi_checks(r: Result, wifi: W.Profile) -> list[Check]:
    """The profile's keyfile is root's with mode 600 and NetworkManager lists it."""
    keyfile = f"600 root /{W.DIR / wifi.filename}"
    return [
        Check(
            "wifi keyfile",
            keyfile,
            "; ".join(r.nm_keyfiles) or "none",
            keyfile in r.nm_keyfiles,
        ),
        Check(
            "wifi connection",
            f"{wifi.ssid} listed by nmcli",
            ", ".join(r.nm_connections) or "none",
            wifi.ssid in r.nm_connections,
        ),
    ]


def _states(r: Result, units: tuple[str, ...], *keys: str) -> str:
    return ", ".join(
        f"{u} " + "/".join(r.boot_units.get(u, {}).get(k, "?") for k in keys)
        for u in units
    )


def _skipped(p: dict[str, str]) -> bool:
    """Condition checked at boot and failed."""
    return (
        p.get("ConditionResult") == "no"
        and int(p.get("ConditionTimestampMonotonic") or 0) > 0
    )


def evaluate(r: Result, overlay: bool) -> list[Check]:
    """Overlay effects present with the overlay, absent without; no unexpected failures."""
    size = zram_swap_bytes(r.mem_total_kib, r.page_size)
    journals = f"/var/log/journal {r.journal_var}, /run/log/journal {r.journal_run}"
    effects = [
        (
            "zram swap",
            f"one /dev/zram* partition of {size} bytes",
            "; ".join(" ".join(map(str, s)) for s in r.swaps) or "none",
            _is_zram_swap(r.swaps, size),
        ),
        (
            "root noatime",
            "noatime in / options",
            ",".join(r.root_options),
            "noatime" in r.root_options,
        ),
        (
            "volatile journal",
            "journal files under /run/log/journal only",
            journals,
            r.journal_var == 0 and r.journal_run > 0,
        ),
        ("plocate timer", "masked", r.plocate_timer, r.plocate_timer == "masked"),
        ("zram-swap.service", "active", r.zram_service, r.zram_service == "active"),
        ("vm.page-cluster", "0", r.page_cluster, r.page_cluster == 0),
        (
            "masked units",
            "masked/inactive: " + ", ".join(MASKED),
            _states(r, MASKED, "LoadState", "ActiveState"),
            all(
                (b := r.boot_units.get(u, {})).get("LoadState") == "masked"
                and b.get("ActiveState") == "inactive"
                for u in MASKED
            ),
        ),
        (
            "udisks2 on demand",
            "inactive after boot, active after a D-Bus call returning its version",
            f"{_states(r, (UDISKS,), 'ActiveState')}; busctl {'; '.join(r.udisks_call) or '-'}; "
            f"then {r.udisks_after}",
            r.boot_units.get(UDISKS, {}).get("ActiveState") == "inactive"
            and re.fullmatch(r'v s "[^"]+"', "".join(r.udisks_call[-1:])) is not None
            and r.udisks_after == "active",
        ),
        (
            "first-boot updates skipped",
            "condition failed: " + ", ".join(UPDATES),
            _states(r, UPDATES, "ConditionResult", "ActiveState"),
            all(_skipped(r.boot_units.get(u, {})) for u in UPDATES),
        ),
    ]
    checks = [
        Check(name, exp if overlay else f"not {exp}", str(obs), present == overlay)
        for name, exp, obs, present in effects
    ]
    nm_state = r.boot_units.get(NM, {}).get("ActiveState", "?")
    checks.append(Check(NM, "active", nm_state, nm_state == "active"))
    unexpected = set(r.failing) - set(EXPECTED_FAILURES)
    checks.append(
        Check(
            "failing units",
            f"only {', '.join(sorted(EXPECTED_FAILURES))}",
            ", ".join(r.failing) or "none",
            not unexpected,
        )
    )
    return checks


def report(r: Result, checks: list[Check]) -> str:
    """Human-readable result."""
    lines = [
        f"{'PASS' if c.ok else 'FAIL'}  {c.name}: {c.observed} (expected {c.expected})"
        for c in checks
    ]
    lines += [
        f"      {u} is expected to fail: {EXPECTED_FAILURES[u]}"
        for u in r.failing
        if u in EXPECTED_FAILURES
    ]
    return "\n".join([*lines, f"system state: {r.state}", r.analyze, *r.blame]) + "\n"


def smoke_key(tar_sha256: str, overlay: Path | None, wifi: str = "") -> str:
    """Log directory key over the tar, the overlay, the Wi-Fi profile id, unit, script."""
    text = f"{tar_sha256} {O.digest(overlay)} {wifi} {unit_file()} {script()} {APPEND}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _wait_socket(sock: Path, daemon: subprocess.Popen) -> None:
    deadline = time.monotonic() + SOCKET_WAIT
    while not sock.exists():
        if daemon.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(
                f"virtiofsd did not create {sock} (exit {daemon.returncode})"
            )
        time.sleep(POLL)


def _run_vm(qemu: list[str], serial: Path, timeout: float) -> None:
    with (
        open(serial, "wb") as out,
        subprocess.Popen(
            qemu,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as vm,
        progress_bar(desc="qemu console", unit=" lines") as progress,
    ):

        def pump():
            for line in vm.stdout:
                out.write(line)
                progress.update()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = vm.wait(timeout)
        except subprocess.TimeoutExpired:
            vm.kill()
            raise TimeoutError(
                f"QEMU still running after {timeout:.0f} s; see {serial}"
            ) from None
        finally:
            reader.join()
    if code:
        raise RuntimeError(f"QEMU exited {code}; see {serial}")


def boot(
    qemu: list[str], daemon: list[str], sock: Path, logs: Path, timeout: float
) -> str:
    """Start daemon, wait for sock, run qemu; return its console from logs/serial.log."""
    serial = logs / "serial.log"
    with (
        open(logs / "virtiofsd.log", "wb") as dlog,
        subprocess.Popen(
            daemon, stdin=subprocess.DEVNULL, stdout=dlog, stderr=dlog
        ) as fsd,
    ):
        try:
            _wait_socket(sock, fsd)
            _run_vm(qemu, serial, timeout)
        finally:
            if fsd.poll() is None:
                fsd.terminate()
    return serial.read_text(errors="replace")


@dataclass
class Outcome:
    """A smoke run: its log directory and what it found."""

    logs: Path
    overlay: bool
    result: Result | None = None
    checks: list[Check] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """Booted, finished and every check passed."""
        return self.result is not None and all(c.ok for c in self.checks)

    def text(self) -> str:
        """Report, or the reason there is none."""
        mode = "overlay" if self.overlay else "no overlay"
        head = f"qemu-smoke ({mode}), logs in {self.logs}\n"
        if self.result is None:
            return head + f"FAIL  {self.error}\n"
        return head + report(self.result, self.checks)


def smoke(
    asset: R.Asset,
    overlay: Path | None,
    cache: Path,
    timeout: float,
    wifi: W.Profile | None = None,
) -> Outcome:
    """Build the flash tree of a rootfs with overlay and Wi-Fi, boot it in QEMU, check it."""
    tar = R.fetch(asset, cache)
    key = smoke_key(asset.sha256, overlay, wifi.uuid if wifi else "")
    logs = Path(cache) / "qemu-smoke" / key
    logs.mkdir(parents=True, exist_ok=True)
    outcome = Outcome(logs, overlay is not None)
    layer = wifi.apply if wifi else None
    with I.rootfs_tree(tar, overlay, "building the QEMU root", layer) as root:
        install(root)
        sock = root.parent / "virtiofsd.sock"
        qemu = qemu_argv(in_tree(root, "vmlinuz"), in_tree(root, "initrd.img"), sock)
        log.info("%s", " ".join(qemu))
        try:
            text = boot(qemu, virtiofsd_argv(root, sock), sock, logs, timeout)
            outcome.result = Result.from_log(text)
            outcome.checks = evaluate(outcome.result, outcome.overlay)
            if wifi:
                outcome.checks += wifi_checks(outcome.result, wifi)
        except (RuntimeError, TimeoutError, ValueError) as e:
            outcome.error = str(e)
    (logs / "result.json").write_text(
        json.dumps(
            {
                "overlay": outcome.overlay,
                "ok": outcome.ok,
                "error": outcome.error,
                "result": asdict(outcome.result) if outcome.result else None,
                "checks": [asdict(c) for c in outcome.checks],
            },
            indent=1,
        )
        + "\n"
    )
    return outcome
