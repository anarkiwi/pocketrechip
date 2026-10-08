"""Verified device steps over the agent: data download, script, status and result upload.

Step k's script clears its status bytes at STAT_ADDR, runs its commands and serves
`stat<k>`, its result as `res<k>`, and the next step's data entity `img<k+1>`, so a
step's data goes down in the session its predecessor opened.
"""

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .fel_agent import STAT_ADDR, Agent, script_text, serve
from .progress import progress_bar

log = logging.getLogger(__name__)

OK = 1
COPY_BLOCK = 1 << 24


@dataclass(frozen=True)
class Data:
    """size bytes of a file from offset, downloaded to the step's address."""

    path: Path
    offset: int
    size: int

    @classmethod
    def file(cls, path: Path) -> "Data":
        """The whole file."""
        return cls(Path(path), 0, Path(path).stat().st_size)

    def materialize(self, tmp: Path) -> Path:
        """The file itself when whole, else the slice copied to tmp."""
        if self.offset == 0 and self.size == self.path.stat().st_size:
            return self.path
        with open(self.path, "rb") as src, open(tmp, "wb") as dst:
            src.seek(self.offset)
            left = self.size
            while left:
                block = src.read(min(left, COPY_BLOCK))
                if not block:
                    raise IOError(f"{self.path} ends before {self.offset + self.size}")
                dst.write(block)
                left -= len(block)
        return tmp


@dataclass(frozen=True)
class Step:
    """A script whose status byte i at STAT_ADDR + i is OK when checks[i] succeeded."""

    name: str
    commands: tuple[str, ...]
    checks: tuple[str, ...]
    data: Data | None = None
    addr: int = 0
    result: int = 0
    strict: bool = True


@dataclass(frozen=True)
class Outcome:
    """Status bytes and uploaded result of a step."""

    step: Step
    status: bytes
    result: bytes | None


class StepFailed(RuntimeError):
    """A strict step reported a failed check."""

    def __init__(self, step: Step, status: bytes):
        bad = next(i for i, s in enumerate(status) if s != OK)
        super().__init__(
            f"step {step.name}: `{step.checks[bad]}` failed (status {list(status)})"
        )
        self.step, self.status = step, status


def chain(checks: Sequence[str], base: int = STAT_ADDR) -> str:
    """Run checks in order while they succeed, marking each success at base + i."""
    text = ""
    for i in reversed(range(len(checks))):
        inner = f"{text}; " if text else ""
        text = f"if {checks[i]}; then mw.b {base + i:#x} {OK}; {inner}fi"
    return text


def chained(
    name: str, checks: Sequence[str], prelude: Iterable[str] = (), **kw
) -> Step:
    """Step running checks in order, stopping at the first failure."""
    return Step(name, (*prelude, chain(checks)), tuple(checks), **kw)


def render(steps: Sequence[Step], seq0: int) -> list[list[str]]:
    """Scripts for steps numbered from seq0."""
    if steps and steps[0].data:
        raise ValueError(f"first step {steps[0].name} cannot carry data")
    out = []
    for i, step in enumerate(steps):
        seq = seq0 + i
        ents = [(f"stat{seq}", STAT_ADDR, len(step.checks))]
        if step.result:
            ents.append((f"res{seq}", step.addr, step.result))
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if nxt and nxt.data:
            ents.append((f"img{seq + 1}", nxt.addr, nxt.data.size))
        out.append(
            [
                f"mw.b {STAT_ADDR:#x} 0 {len(step.checks):#x}",
                *step.commands,
                serve(ents),
            ]
        )
    return out


def write_scripts(steps: Sequence[Step], out: Path, seq0: int = 1) -> list[Path]:
    """Rendered script sources, one file per step."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (step, cmds) in enumerate(zip(steps, render(steps, seq0))):
        path = out / f"{seq0 + i:04d}-{step.name}.cmd"
        path.write_text(script_text(cmds))
        paths.append(path)
    return paths


def run(agent: Agent, steps: Sequence[Step], desc: str = "steps") -> list[Outcome]:
    """Run steps on a booted agent; raises StepFailed when a strict step fails."""
    seq0 = agent.seq + 1
    scripts = render(steps, seq0)
    agent.seq += len(steps)
    total = sum(s.data.size for s in steps if s.data)
    outcomes = []
    t0, done = time.monotonic(), 0
    with progress_bar(
        total=total, unit="B", unit_scale=True, unit_divisor=1024, desc=desc
    ) as progress:
        for i, (step, cmds) in enumerate(zip(steps, scripts)):
            seq = seq0 + i
            work = agent.workdir
            if step.data:
                tmp = work / f"img{seq}.bin"
                path = step.data.materialize(tmp)
                agent.dfu.download(f"img{seq}", path, detach=False)
                if path == tmp:
                    tmp.unlink()
            agent.run(f"step{seq}-{step.name}", cmds, f"stat{seq}")
            spath = agent.dfu.upload(f"stat{seq}", work / "stat.bin", len(step.checks))
            status = spath.read_bytes()
            spath.unlink()
            result = None
            if step.result:
                rpath = agent.dfu.upload(f"res{seq}", work / "res.bin", step.result)
                result = rpath.read_bytes()
                rpath.unlink()
            if step.strict and any(s != OK for s in status):
                raise StepFailed(step, status)
            outcomes.append(Outcome(step, status, result))
            if step.data:
                done += step.data.size
                progress.update(step.data.size)
            rate = done / max(time.monotonic() - t0, 1e-9)
            log.info(
                "step %d/%d %s: status %s; %.2f MB/s, ETA %s",
                i + 1,
                len(steps),
                step.name,
                list(status),
                rate / 1e6,
                tqdm.format_interval((total - done) / rate) if rate else "?",
            )
    return outcomes
