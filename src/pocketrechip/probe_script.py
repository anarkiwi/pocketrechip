"""Generate the read-only U-Boot NAND probe script from the window layout."""

from . import probe_layout as L


def _guarded(cmd: str, flag_off: int) -> str:
    flag = hex(L.WINDOW_BASE + flag_off)
    return f"if {cmd}; then mw.b {flag} {L.STATUS_OK}; else mw.b {flag} {L.STATUS_FAILED}; fi"


def commands(dfu_sessions: int = L.DFU_SESSIONS) -> list[str]:
    """U-Boot commands: NAND reads into the window, then serve it over DFU and reset."""
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
        f"setenv dfu_alt_info 'probe ram {hex(base)} {hex(L.WINDOW_LEN)}'",
        "; ".join(["dfu 0 ram 0"] * dfu_sessions),
        "reset",
    ]
    return out


def script(dfu_sessions: int = L.DFU_SESSIONS) -> str:
    """Probe script text as written to probe.cmd."""
    return "".join(c + "\n" for c in commands(dfu_sessions))
