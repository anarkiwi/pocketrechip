"""Command line entry point."""

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    analyze,
    backup,
    flash,
    overlay,
    probe_script,
    qemu_smoke,
    release,
    restore,
)
from .fel_agent import Agent
from .probe_layout import CHIP_BY_KEY, NAND_CHIPS
from .progress import setup_logging

UBOOT = Path("/opt/pocketrechip/u-boot-sunxi-with-spl.bin")
CACHE = Path(os.environ.get("POCKETRECHIP_CACHE", "cache"))
DONE = (
    "flash complete; the board was reset and is back in FEL: "
    "remove the FEL jumper and power-cycle it to boot from NAND"
)


def _print_summary(summary: analyze.ProbeSummary, as_json: bool) -> int:
    if as_json:
        json.dump(analyze.to_dict(summary), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(analyze.report(summary))
    return 0 if summary.finished else 1


def _device_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, required=True, help="output directory")
    p.add_argument(
        "--uboot", type=Path, default=UBOOT, help="probe U-Boot image (read-only agent)"
    )
    p.add_argument(
        "--timeout", type=float, default=300.0, help="seconds to wait per DFU session"
    )


def _agent(args: argparse.Namespace) -> Agent:
    return Agent(args.out / backup.WORK_DIR, timeout=args.timeout)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pocketrechip")
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser(
        "probe-script", help="print the read-only U-Boot NAND probe script"
    )
    ps.add_argument("--seq", type=int, default=1, help="DFU entity sequence number")
    pa = sub.add_parser("analyze", help="decode a probe dram.bin")
    pa.add_argument("dram", help="path to dram.bin")
    pa.add_argument("--json", action="store_true", help="emit JSON")
    pp = sub.add_parser("probe", help="run the probe over FEL and decode it")
    _device_args(pp)
    pp.add_argument("--json", action="store_true", help="emit JSON")
    pb = sub.add_parser("backup", help="back up the whole NAND over FEL")
    _device_args(pb)
    pb.add_argument("--chunk-ebs", type=int, default=16, help="eraseblocks per session")
    pb.add_argument(
        "--oob", type=int, help="OOB bytes per page (default from detected chip)"
    )
    pb.add_argument("--eraseblocks", type=int, help="default from detected chip")
    pb.add_argument(
        "--verify", action="store_true", help="re-read and compare ECC data"
    )
    pf = sub.add_parser("flash", help="install the Debian release on NAND over FEL")
    _device_args(pf)
    pf.add_argument("--backup", type=Path, required=True, help="complete backup dir")
    pf.add_argument(
        "--no-backup", action="store_true", help="flash without a matching backup"
    )
    pf.add_argument("--flavor", choices=sorted(release.ROOTFS), default="pocketchip")
    pf.add_argument("--cache", type=Path, default=CACHE, help="download/image cache")
    pf.add_argument("--chunk-mib", type=int, default=flash.CHUNK >> 20)
    pf.add_argument(
        "--overlay", type=Path, default=overlay.default(), help="rootfs overlay dir"
    )
    pf.add_argument("--no-overlay", action="store_true", help="stock rootfs")
    pf.add_argument("--dry-run", action="store_true", help="host side only, no USB")
    pf.add_argument(
        "--chip", choices=sorted(CHIP_BY_KEY), help="dry-run chip (default: backup's)"
    )
    pr = sub.add_parser("restore", help="write a backup back to NAND over FEL")
    _device_args(pr)
    pr.add_argument("--backup", type=Path, required=True, help="complete backup dir")
    pr.add_argument("--chunk-ebs", type=int, default=16, help="eraseblocks per session")
    pr.add_argument(
        "--verify", action="store_true", help="re-read and compare ECC data"
    )
    pq = sub.add_parser(
        "qemu-smoke", help="boot the flash rootfs under QEMU and check the overlay"
    )
    pq.add_argument("--flavor", choices=sorted(release.ROOTFS), default="pocketchip")
    pq.add_argument("--cache", type=Path, default=CACHE, help="download/log cache")
    pq.add_argument(
        "--overlay", type=Path, default=overlay.default(), help="rootfs overlay dir"
    )
    pq.add_argument("--no-overlay", action="store_true", help="stock rootfs baseline")
    pq.add_argument(
        "--timeout", type=float, default=3600.0, help="seconds until QEMU is killed"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the pocketrechip CLI."""
    args = _parser().parse_args(argv)
    setup_logging()
    if args.cmd == "qemu-smoke":
        return _qemu_smoke(args)
    if args.cmd == "probe-script":
        sys.stdout.write(probe_script.script(args.seq))
        return 0
    if args.cmd == "analyze":
        return _print_summary(analyze.load(args.dram), args.json)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "flash":
        return _flash(args)
    return _device(args)


def _device(args: argparse.Namespace) -> int:
    agent = _agent(args)
    if args.cmd == "probe":
        with agent.session(args.uboot):
            dram = probe_script.capture(agent, args.out)
        return _print_summary(analyze.load(dram), args.json)
    if args.cmd == "restore":
        res = restore.restore(
            agent, args.uboot, args.backup, args.chunk_ebs, args.verify
        )
        print(json.dumps(res))
        return 1 if res["failed"] or res["mismatch"] else 0
    opts = backup.Options(args.chunk_ebs, args.oob, args.eraseblocks, args.verify)
    manifest = backup.backup(agent, args.out, args.uboot, opts)
    print(backup.summary(manifest))
    return 0


def _qemu_smoke(args: argparse.Namespace) -> int:
    tar = release.ROOTFS[args.flavor]
    outcome = qemu_smoke.smoke(
        release.fetch(tar, args.cache),
        tar.sha256,
        None if args.no_overlay else args.overlay,
        args.cache,
        args.timeout,
    )
    print(outcome.text(), end="")
    return 0 if outcome.ok else 1


def _flash(args: argparse.Namespace) -> int:
    lay = None if args.no_overlay else args.overlay
    prep = flash.prepare(args.cache, args.flavor, args.out, lay)
    chunk = args.chunk_mib << 20
    if args.dry_run:
        m = None if args.no_backup else flash.check_backup(args.backup)
        if args.chip:
            chips = [CHIP_BY_KEY[args.chip]]
        elif m:
            chips = [c for c in NAND_CHIPS.values() if c.name == m["chip"]]
        else:
            chips = list(NAND_CHIPS.values())
        print(flash.dry_run(prep, chips, args.out, chunk), end="")
        return 0
    backup_dir = None if args.no_backup else args.backup
    chip = flash.flash(_agent(args), args.uboot, prep, backup_dir, chunk)
    print(f"{chip.name}: {DONE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
