"""One-command reflash: wait for the board in FEL, back up its NAND, flash it.

State lives per SoC SID under <cache>/devices/<sid>/, so several PocketCHIPs never share
a backup.
"""

import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from . import backup as B
from . import flash as F
from .fel_agent import Agent

DEVICES, BACKUP, FLASH = "devices", "backup", "flash"
HOST_CACHE = "POCKETRECHIP_HOST_CACHE"
SID_RE = re.compile(r"^([0-9a-f]{8}(?::[0-9a-f]{8}){3})$", re.M)
CONNECT = """\
Put the PocketCHIP in FEL mode:
  1. Switch it off (hold the power button for 8 seconds).
  2. Jumper the FEL pad to the GROUND pad beside it (the two right-most pads on the
     top edge header).
  3. Connect it to this computer by USB; press the power button if it stays off.
Waiting up to {timeout:.0f} s for it in FEL..."""
REBOOT = (
    "remove the FEL jumper, then power-cycle it "
    "(unplug USB, hold the power button for 8 seconds, press it again)"
)
LOGIN = (
    "Log in as `chip`, password `chip` (member of sudo); "
    "change the password with `passwd` on first login."
)


@dataclass(frozen=True)
class Options:
    """Device options for install."""

    uboot: Path
    timeout: float = 300.0
    backup: bool = True
    verify_backup: bool = False
    chunk_ebs: int = 16
    chunk: int = F.CHUNK
    wifi_ssid: str | None = None


class Phases:
    """Numbered phase headers and their durations."""

    def __init__(self, total: int):
        self.total = total
        self.done: list[tuple[str, float]] = []

    @contextmanager
    def __call__(self, title: str) -> Iterator[None]:
        print(f"\n[{len(self.done) + 1}/{self.total}] {title}", flush=True)
        t0 = time.monotonic()
        yield
        self.done.append((title, time.monotonic() - t0))

    def text(self) -> str:
        """Total and per-phase durations."""
        fmt = tqdm.format_interval
        each = ", ".join(f"{t.lower()} {fmt(s)}" for t, s in self.done)
        return f"{fmt(sum(s for _, s in self.done))} ({each})"


def sid(agent: Agent) -> str:
    """SoC SID key of the board in FEL, as `sunxi-fel sid` prints it."""
    out = agent.runner(["sunxi-fel", "sid"])
    m = SID_RE.search(out)
    if not m:
        raise ValueError(f"no SID in `sunxi-fel sid` output {out.strip()!r}")
    return m[1]


def device_dir(cache: Path, key: str) -> Path:
    """Per-device state directory for a SID."""
    return Path(cache) / DEVICES / key.replace(":", "-")


def connect(cache: Path, timeout: float) -> Path:
    """Wait for a board in FEL; its state directory."""
    print(CONNECT.format(timeout=timeout), flush=True)
    agent = Agent(Path(cache) / DEVICES, timeout=timeout)
    agent.wait_fel()
    out = device_dir(cache, sid(agent))
    print(f"found SID {out.name}", flush=True)
    return out


def shown(path: Path, cache: Path) -> Path:
    """path as the host sees it when cache is a container mount of HOST_CACHE."""
    host = os.environ.get(HOST_CACHE)
    return Path(host) / Path(path).relative_to(cache) if host else Path(path)


def install(
    cache: Path,
    prepare: Callable[[], AbstractContextManager[F.Prepared]],
    opts: Options,
) -> str:
    """Prepare the image, then back up and flash the board in FEL; the closing text."""
    phases = Phases(4)
    with ExitStack() as stack:
        with phases("Image"):
            prep = stack.enter_context(prepare())
        with phases("Connect"):
            dev = connect(cache, opts.timeout)
        bdir = dev / BACKUP
        manifest = None
        with phases("Backup"):
            if opts.backup:
                agent = Agent(bdir / B.WORK_DIR, timeout=opts.timeout)
                bopts = B.Options(opts.chunk_ebs, verify=opts.verify_backup)
                manifest = B.backup(agent, bdir, opts.uboot, bopts)
                print(B.summary(manifest), flush=True)
            else:
                print("skipped (--no-backup)", flush=True)
        with phases("Flash"):
            agent = Agent(dev / FLASH / B.WORK_DIR, timeout=opts.timeout)
            agent.wait_fel()
            backup = bdir if manifest else None
            chip = F.flash(agent, opts.uboot, prep, backup, opts.chunk)
    kept = (
        f"Backup of the original NAND: {shown(bdir, cache)} "
        "(`./install.sh restore` writes it back)."
        if manifest
        else "No backup was taken (--no-backup)."
    )
    net = (
        f"On first boot it joins Wi-Fi {opts.wifi_ssid!r} and runs openssh-server: "
        "find its address from your router or with `ip a` on the device, then "
        "`ssh chip@<address>`.\n"
        if opts.wifi_ssid
        else ""
    )
    return (
        f"\nDone in {phases.text()}.\n"
        f"PocketCHIP {dev.name} ({chip.name}) now has Debian trixie ({prep.flavor}).\n"
        f"{kept}\nNext: {REBOOT}.\n{LOGIN}\n{net}"
    )
