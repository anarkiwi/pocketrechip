"""Generate the read-only U-Boot NAND probe script and capture its window."""

from pathlib import Path

from . import probe_layout as L
from .fel_agent import STAT_ADDR, Agent, script_text, serve

DETECT_READS = (
    (0, L.WINDOW_BASE + L.EB_PAGES_OFF),
    *((r.nand_off, r.addr) for r in L.SIZE_PROBES),
)
DETECT_LEN = L.NFC_LEN + len(DETECT_READS)


def _guarded(cmd: str, flag_addr: int) -> str:
    flag = hex(flag_addr)
    return f"if {cmd}; then mw.b {flag} {L.STATUS_OK}; else mw.b {flag} {L.STATUS_FAILED}; fi"


def commands(seq: int = 1) -> list[str]:
    """Agent script: NAND reads into the window, then expose it as alt probe<seq>."""
    base = L.WINDOW_BASE
    out = [f"mw.b {hex(base)} 0 {hex(L.CLEAR_LEN)}"]
    for i, r in enumerate(L.REGIONS):
        if r.raw:
            args = f"read.raw {hex(r.addr)} {hex(r.nand_off) if r.nand_off else 0} 1"
        else:
            args = f"read {hex(r.addr)} {hex(r.nand_off)} {hex(r.size)}"
        out.append(_guarded(f"nand {args}", base + L.STATUS_OFF + i))
    for n in range(L.N_ERASEBLOCKS):
        dst = base + L.EB_PAGES_OFF + n * L.PAGE
        out.append(
            _guarded(
                f"nand read {hex(dst)} {hex(n * L.ERASEBLOCK)} {hex(L.PAGE)}",
                base + L.EB_STATUS_OFF + n,
            )
        )
    out += [
        f"cp.l {hex(L.NFC_BASE)} {hex(base + L.NFC_OFF)} {hex(L.NFC_LEN // 4)}",
        f"mw.l {hex(base + L.DONE_OFF)} {hex(L.DONE_MAGIC)}",
        serve([(f"probe{seq}", base, L.WINDOW_LEN)]),
    ]
    return out


def script(seq: int = 1) -> str:
    """Probe script text."""
    return script_text(commands(seq))


def capture(agent: Agent, out: Path) -> Path:
    """Run the probe on a booted agent and upload its window to out/dram.bin."""
    seq = agent.next_seq()
    agent.run(f"probe{seq}", commands(seq), f"probe{seq}")
    return agent.dfu.upload(f"probe{seq}", Path(out) / "dram.bin", L.WINDOW_LEN)


def detect_commands(seq: int, base: int = STAT_ADDR) -> list[str]:
    """Agent script: NFC registers at base after a page-0 read, then the size probes.

    Status bytes for the reads in DETECT_READS follow the registers at base + NFC_LEN.
    """
    reads = [
        _guarded(f"nand read {dst:#x} {off:#x} {L.PAGE:#x}", base + L.NFC_LEN + i)
        for i, (off, dst) in enumerate(DETECT_READS)
    ]
    return [
        f"mw.b {base:#x} 0 {DETECT_LEN:#x}",
        reads[0],
        f"cp.l {L.NFC_BASE:#x} {base:#x} {L.NFC_LEN // 4:#x}",
        *reads[1:],
        serve([(f"detect{seq}", base, DETECT_LEN)]),
    ]


def detect(agent: Agent) -> tuple[int | None, int]:
    """NAND size from the size probes and the NAND ID byte, on a booted agent."""
    seq = agent.next_seq()
    alt = f"detect{seq}"
    agent.run(alt, detect_commands(seq), alt)
    buf = agent.dfu.upload(alt, agent.workdir / "detect.bin", DETECT_LEN).read_bytes()
    return L.nand_size(buf[L.NFC_LEN + 1 :]), buf[L.NFC_ID_BYTE]
