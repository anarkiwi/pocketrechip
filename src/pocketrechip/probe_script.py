"""Generate the read-only U-Boot NAND probe script and capture its window."""

from pathlib import Path

from . import probe_layout as L
from .fel_agent import Agent, script_text, serve


def _guarded(cmd: str, flag_off: int) -> str:
    flag = hex(L.WINDOW_BASE + flag_off)
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
        out.append(_guarded(f"nand {args}", L.STATUS_OFF + i))
    for n in range(L.N_ERASEBLOCKS):
        dst = base + L.EB_PAGES_OFF + n * L.PAGE
        out.append(
            _guarded(
                f"nand read {hex(dst)} {hex(n * L.ERASEBLOCK)} {hex(L.PAGE)}",
                L.EB_STATUS_OFF + n,
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
