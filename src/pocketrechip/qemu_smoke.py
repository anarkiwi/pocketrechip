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
JOURNAL_FILES = "find {} -type f -name '*.journal*' 2>/dev/null | wc -l"
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
    "units": "systemctl show -p Id,ActiveState,Result,NRestarts '*'"
    ' | awk \'BEGIN { RS = ""; FS = "\\n" } { $1 = $1; print }\'',
    "analyze": "systemd-analyze",
    "free": "free -k",
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


def fstab(text: str) -> str:
    """fstab with the `/` entry moved to the virtiofs tag, its options kept."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith("#") and f[1] == "/":
            line = f"{TAG} / virtiofs {f[3]} 0 0"
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


def failing(units: list[str]) -> tuple[str, ...]:
    """Units failed, with a non-success result, or restarted after failing."""
    out = []
    for line in units:
        p = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
        if "Id" in p and (
            p.get("ActiveState") == "failed"
            or p.get("Result", "success") != "success"
            or int(p.get("NRestarts") or 0)
        ):
            out.append(p["Id"])
    return tuple(sorted(out))


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
    ]
    checks = [
        Check(name, exp if overlay else f"not {exp}", str(obs), present == overlay)
        for name, exp, obs, present in effects
    ]
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
    return "\n".join([*lines, f"system state: {r.state}", r.analyze]) + "\n"


def smoke_key(tar_sha256: str, overlay: Path | None) -> str:
    """Log directory key over the tar, the overlay, the test unit and script."""
    text = f"{tar_sha256} {O.digest(overlay)} {unit_file()} {script()} {APPEND}"
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
    tar: Path, tar_sha256: str, overlay: Path | None, cache: Path, timeout: float
) -> Outcome:
    """Build the flash tree of tar with overlay, boot it under QEMU and evaluate it."""
    logs = Path(cache) / "qemu-smoke" / smoke_key(tar_sha256, overlay)
    logs.mkdir(parents=True, exist_ok=True)
    outcome = Outcome(logs, overlay is not None)
    with I.rootfs_tree(tar, overlay, "building the QEMU root") as root:
        install(root)
        sock = root.parent / "virtiofsd.sock"
        qemu = qemu_argv(in_tree(root, "vmlinuz"), in_tree(root, "initrd.img"), sock)
        log.info("%s", " ".join(qemu))
        try:
            text = boot(qemu, virtiofsd_argv(root, sock), sock, logs, timeout)
            outcome.result = Result.from_log(text)
            outcome.checks = evaluate(outcome.result, outcome.overlay)
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
