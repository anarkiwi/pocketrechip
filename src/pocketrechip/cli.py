"""Command line entry point."""

import argparse
import json
import sys
from pathlib import Path

from . import analyze, backup, probe_script
from .fel_agent import Agent

UBOOT = Path("/opt/pocketrechip/u-boot-sunxi-with-spl.bin")


def _print_summary(summary: analyze.ProbeSummary, as_json: bool) -> int:
    if as_json:
        json.dump(analyze.to_dict(summary), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(analyze.report(summary))
    return 0 if summary.finished else 1


def _device_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, required=True, help="output directory")
    p.add_argument("--uboot", type=Path, default=UBOOT, help="probe U-Boot image")
    p.add_argument(
        "--timeout", type=float, default=300.0, help="seconds to wait per DFU session"
    )


def _agent(args: argparse.Namespace) -> Agent:
    return Agent(args.out / backup.WORK_DIR, timeout=args.timeout)


def main(argv: list[str] | None = None) -> int:
    """Run the pocketrechip CLI."""
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
    pb.add_argument("--oob", type=int, help="OOB bytes per page (default from NAND ID)")
    pb.add_argument("--eraseblocks", type=int, help="default from NAND ID")
    pb.add_argument(
        "--verify", action="store_true", help="re-read and compare ECC data"
    )
    args = p.parse_args(argv)
    if args.cmd == "probe-script":
        sys.stdout.write(probe_script.script(args.seq))
        return 0
    if args.cmd == "analyze":
        return _print_summary(analyze.load(args.dram), args.json)
    args.out.mkdir(parents=True, exist_ok=True)
    agent = _agent(args)
    if args.cmd == "probe":
        with agent.session(args.uboot):
            dram = probe_script.capture(agent, args.out)
        return _print_summary(analyze.load(dram), args.json)
    opts = backup.Options(args.chunk_ebs, args.oob, args.eraseblocks, args.verify)
    manifest = backup.backup(agent, args.out, args.uboot, opts)
    print(backup.summary(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
