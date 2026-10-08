"""Host side of a U-Boot agent loop serving scripts and DRAM over USB DFU.

The FEL boot script loops: serve the `cmd` entity, source what was downloaded, clear it.
Sessions end with DFU_DETACH alone: a bus reset after detach (`dfu-util -R`) makes
`run_usb_dnl_gadget` (common/dfu.c) reset the board instead of returning.
"""

import re
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .probe_layout import SCRIPT_ADDR

CMD_ADDR = 0x43200000
CMD_LEN = 0x100000
STAT_ADDR = 0x43300000
CMD_ALT = "cmd"

DFU_ID = "1f3a:1010"

WDT_CTRL, WDT_MODE = 0x01C20C90, 0x01C20C94
WDT_MODE_RESET = 0x3
WDT_CTRL_RESTART = (0xA57 << 1) | 1

Runner = Callable[[Sequence[str]], str]

FOUND_RE = re.compile(rf'^Found DFU: \[{DFU_ID}\] .*\balt=(\d+), name="([^"]*)"', re.M)


class ToolError(subprocess.CalledProcessError):
    """CalledProcessError whose message carries the tool's stderr."""

    def __str__(self) -> str:
        return f"{super().__str__()}: {(self.stderr or '').strip()}"


def subprocess_runner(argv: Sequence[str]) -> str:
    """Run a tool, raising ToolError on failure; returns stdout."""
    res = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    if res.returncode:
        raise ToolError(res.returncode, res.args, res.stdout, res.stderr)
    return res.stdout


def alt_info(entities: Iterable[tuple[str, int, int]]) -> str:
    """dfu_alt_info for the cmd entity followed by (name, addr, size) RAM entities."""
    ents = [(CMD_ALT, CMD_ADDR, CMD_LEN), *entities]
    return ";".join(f"{n} ram {a:#x} {s:#x}" for n, a, s in ents)


def serve(entities: Iterable[tuple[str, int, int]]) -> str:
    """Command exposing entities in the next DFU session."""
    return f"setenv dfu_alt_info '{alt_info(entities)}'"


def reset_commands() -> list[str]:
    """Watchdog reset that keeps rewriting WDT_MODE, as the sun5i reset_cpu() does.

    WDT_MODE = RESET_EN | EN with the 0.5 s interval, then WDT_CTRL = KEY | RESTART.
    The sysreset path (sunxi_wdt_expire_now) arms the watchdog once, which can hang sun5i.
    """
    mode = f"mw.l {WDT_MODE:#x} {WDT_MODE_RESET}"
    return [
        mode,
        f"mw.l {WDT_CTRL:#x} {WDT_CTRL_RESTART:#x}",
        f"while itest 1 == 1; do {mode}; done",
    ]


def boot_commands() -> list[str]:
    """FEL boot script: the agent loop, never falling through to bootcmd."""
    return [
        serve(()),
        f"while itest 1 == 1; do dfu 0 ram 0; source {CMD_ADDR:#x}; "
        f"mw.l {CMD_ADDR:#x} 0 4; done",
        *reset_commands(),
    ]


def script_text(commands: Iterable[str]) -> str:
    """Script source, one command per line."""
    return "".join(c + "\n" for c in commands)


def mkimage(text: str, path: Path, runner: Runner = subprocess_runner) -> Path:
    """Write text to path.with_suffix('.cmd') and wrap it as a legacy script image."""
    src = path.with_suffix(".cmd")
    src.write_text(text)
    runner(
        ["mkimage", "-A", "arm", "-O", "linux", "-T", "script", "-C", "none"]
        + ["-n", path.stem, "-d", str(src), str(path)]
    )
    return path


class Dfu:
    """dfu-util wrapper for the agent's DFU gadget."""

    def __init__(
        self,
        runner: Runner | None = None,
        poll: float = 0.5,
        attempts: int = 3,
        retry_timeout: float = 30.0,
    ):
        self.runner = runner or subprocess_runner
        self.poll = poll
        self.attempts = attempts
        self.retry_timeout = retry_timeout

    def _dfu(self, *args: str) -> str:
        return self.runner(["dfu-util", "-d", DFU_ID, *args])

    def alts(self) -> list[str]:
        """Alt names currently enumerated, in alt order."""
        try:
            out = self.runner(["dfu-util", "-l"])
        except subprocess.CalledProcessError:
            return []
        return [n for _, n in sorted((int(a), n) for a, n in FOUND_RE.findall(out))]

    def wait_alt(self, name: str, timeout: float) -> list[str]:
        """Poll until an alt called name is enumerated."""
        deadline = time.monotonic() + timeout
        while True:
            alts = self.alts()
            if name in alts:
                return alts
            if time.monotonic() > deadline:
                raise TimeoutError(f"DFU alt {name!r} not seen in {timeout}s: {alts}")
            time.sleep(self.poll)

    def download(self, alt: str, path: Path, detach: bool = True) -> None:
        """Download a file to an alt, then end the session with DFU_DETACH."""
        self._dfu("-a", alt, "-D", str(path))
        if detach:
            self._dfu("-a", alt, "-e")

    def _upload_once(self, alt: str, path: Path, size: int) -> Path:
        path.unlink(missing_ok=True)
        self._dfu("-a", alt, "-U", str(path), "-Z", str(size))
        got = path.stat().st_size if path.exists() else 0
        if got != size:
            raise IOError(f"{alt}: uploaded {got:#x} bytes, expected {size:#x}")
        return path

    def upload(self, alt: str, path: Path, size: int) -> Path:
        """Upload an alt of known size to path, re-waiting for the alt between attempts."""
        for _ in range(self.attempts - 1):
            try:
                return self._upload_once(alt, path, size)
            except (subprocess.CalledProcessError, IOError):
                self.wait_alt(alt, self.retry_timeout)
        return self._upload_once(alt, path, size)


class Agent:
    """FEL-booted U-Boot agent: runs scripts, exposes DRAM over DFU."""

    def __init__(
        self,
        workdir: Path,
        runner: Runner | None = None,
        timeout: float = 300.0,
        poll: float = 0.5,
    ):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.runner = runner or subprocess_runner
        self.timeout = timeout
        self.dfu = Dfu(self.runner, poll)
        self.seq = 0

    def boot(self, uboot: Path) -> None:
        """FEL-load U-Boot with the agent loop and wait for the cmd entity."""
        scr = mkimage(
            script_text(boot_commands()), self.workdir / "agent.scr", self.runner
        )
        self.runner(
            [
                "sunxi-fel",
                "-p",
                "uboot",
                str(uboot),
                "write",
                hex(SCRIPT_ADDR),
                str(scr),
            ]
        )
        self.dfu.wait_alt(CMD_ALT, self.timeout)

    def next_seq(self) -> int:
        """Sequence number for the next script's entity names."""
        self.seq += 1
        return self.seq

    def run(self, name: str, commands: Iterable[str], wait: str | None) -> None:
        """Download and source a script; wait for the alt it exposes."""
        scr = mkimage(script_text(commands), self.workdir / f"{name}.scr", self.runner)
        if scr.stat().st_size > CMD_LEN:
            raise ValueError(f"{scr} exceeds the cmd entity ({CMD_LEN:#x} bytes)")
        self.dfu.download(CMD_ALT, scr)
        if wait is not None:
            self.dfu.wait_alt(wait, self.timeout)

    def reset(self) -> None:
        """Reset the board (back to FEL while the FEL pin is grounded)."""
        self.run("reset", reset_commands(), None)

    @contextmanager
    def session(self, uboot: Path) -> Iterator["Agent"]:
        """Boot the agent; reset the board on exit, best effort after an error."""
        self.boot(uboot)
        try:
            yield self
        except BaseException:
            try:
                self.reset()
            except (subprocess.CalledProcessError, OSError, ValueError):
                pass
            raise
        self.reset()
