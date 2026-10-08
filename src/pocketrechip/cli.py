"""Command line entry point."""

import argparse
import json
import sys

from . import analyze, probe_script


def main(argv: list[str] | None = None) -> int:
    """Run the pocketrechip CLI."""
    p = argparse.ArgumentParser(prog="pocketrechip")
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser(
        "probe-script", help="print the read-only U-Boot NAND probe script"
    )
    ps.add_argument("--dfu-sessions", type=int, default=probe_script.L.DFU_SESSIONS)
    pa = sub.add_parser("analyze", help="decode a probe dram.bin")
    pa.add_argument("dram", help="path to dram.bin")
    pa.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args(argv)
    if args.cmd == "probe-script":
        sys.stdout.write(probe_script.script(args.dfu_sessions))
        return 0
    summary = analyze.load(args.dram)
    if args.json:
        json.dump(analyze.to_dict(summary), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(analyze.report(summary))
    return 0 if summary.finished else 1


if __name__ == "__main__":
    sys.exit(main())
