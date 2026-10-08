"""Command line entry point."""

import argparse
import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from . import (
    analyze,
    backup,
    flash,
    install,
    overlay,
    probe_script,
    qemu_smoke,
    release,
    restore,
    wifi,
)
from .fel_agent import Agent
from .probe_layout import CHIP_BY_KEY, NAND_CHIPS
from .progress import setup_logging

UBOOT = Path("/opt/pocketrechip/u-boot-sunxi-with-spl.bin")
CACHE = Path(os.environ.get("POCKETRECHIP_CACHE", "cache"))
DONE = f"flash complete; the board was reset and is back in FEL: {install.REBOOT}"


def _print_summary(summary: analyze.ProbeSummary, as_json: bool) -> int:
    if as_json:
        json.dump(analyze.to_dict(summary), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(analyze.report(summary))
    return 0 if summary.finished else 1


def _usb_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--uboot", type=Path, default=UBOOT, help="probe U-Boot image (read-only agent)"
    )
    p.add_argument(
        "--timeout", type=float, default=300.0, help="seconds to wait per DFU session"
    )


def _device_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--out", type=Path, required=required, help="output directory")
    _usb_args(p)


def _chunk_ebs(p: argparse.ArgumentParser) -> None:
    p.add_argument("--chunk-ebs", type=int, default=16, help="eraseblocks per session")


def _image_args(p: argparse.ArgumentParser, prepare: bool = True) -> None:
    p.add_argument("--flavor", choices=sorted(release.ROOTFS), default="pocketchip")
    p.add_argument("--cache", type=Path, default=CACHE, help="download/image cache")
    p.add_argument(
        "--overlay", type=Path, default=overlay.default(), help="rootfs overlay dir"
    )
    p.add_argument("--no-overlay", action="store_true", help="stock rootfs")
    p.add_argument("--wifi", metavar="SSID", help="Wi-Fi network to join on boot")
    p.add_argument(
        "--wifi-password-file",
        type=Path,
        metavar="FILE",
        help=f"Wi-Fi password file (default: ${wifi.ENV}, else a prompt)",
    )
    p.add_argument("--wifi-open", action="store_true", help="open Wi-Fi network")
    if prepare:
        p.add_argument(
            "--prepare-only",
            action="store_true",
            help="fetch the release and build the image (as root), no USB",
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
    pi = sub.add_parser(
        "install", help="back up and flash the board in FEL, state kept per SoC SID"
    )
    _usb_args(pi)
    _image_args(pi)
    _chunk_ebs(pi)
    pi.add_argument("--no-backup", action="store_true", help="flash without a backup")
    pi.add_argument(
        "--verify-backup", action="store_true", help="re-read and compare ECC data"
    )
    pb = sub.add_parser("backup", help="back up the whole NAND over FEL")
    _device_args(pb)
    _chunk_ebs(pb)
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
    _image_args(pf)
    pf.add_argument("--chunk-mib", type=int, default=flash.CHUNK >> 20)
    pf.add_argument("--dry-run", action="store_true", help="host side only, no USB")
    pf.add_argument(
        "--chip", choices=sorted(CHIP_BY_KEY), help="dry-run chip (default: backup's)"
    )
    pr = sub.add_parser("restore", help="write a backup back to NAND over FEL")
    _device_args(pr, required=False)
    pr.add_argument(
        "--backup", type=Path, help="complete backup dir (default: the board's install)"
    )
    pr.add_argument("--cache", type=Path, default=CACHE, help="install state cache")
    _chunk_ebs(pr)
    pr.add_argument(
        "--verify", action="store_true", help="re-read and compare ECC data"
    )
    pq = sub.add_parser(
        "qemu-smoke", help="boot the flash rootfs under QEMU and check the overlay"
    )
    _image_args(pq, prepare=False)
    pq.add_argument(
        "--timeout", type=float, default=3600.0, help="seconds until QEMU is killed"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the pocketrechip CLI."""
    args = _parser().parse_args(argv)
    setup_logging()
    if getattr(args, "prepare_only", False):
        with TemporaryDirectory(prefix="pocketrechip-") as tmp:
            _prepare(args, Path(tmp))
        return 0
    host = {
        "qemu-smoke": _qemu_smoke,
        "probe-script": _probe_script,
        "analyze": lambda a: _print_summary(analyze.load(a.dram), a.json),
        "install": _install,
    }
    if args.cmd in host:
        return host[args.cmd](args)
    if args.cmd == "restore" and args.backup is None:
        args.backup = install.connect(args.cache, args.timeout) / install.BACKUP
    args.out = args.out or args.backup
    args.out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "flash":
        return _flash(args)
    return _device(args)


def _probe_script(args: argparse.Namespace) -> int:
    sys.stdout.write(probe_script.script(args.seq))
    return 0


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
        if res["failed"] or res["mismatch"]:
            return 1
        print(f"restore complete: {install.REBOOT}")
        return 0
    opts = backup.Options(args.chunk_ebs, args.oob, args.eraseblocks, args.verify)
    manifest = backup.backup(agent, args.out, args.uboot, opts)
    print(backup.summary(manifest))
    return 0


def _wifi(args: argparse.Namespace) -> wifi.Profile | None:
    return wifi.profile(args.wifi, args.wifi_open, args.wifi_password_file)


def _qemu_smoke(args: argparse.Namespace) -> int:
    outcome = qemu_smoke.smoke(
        release.ROOTFS[args.flavor],
        None if args.no_overlay else args.overlay,
        args.cache,
        args.timeout,
        _wifi(args),
    )
    print(outcome.text(), end="")
    return 0 if outcome.ok else 1


def _prepare(args: argparse.Namespace, out: Path) -> flash.Prepared:
    lay = None if args.no_overlay else args.overlay
    return flash.prepare(args.cache, args.flavor, out, lay)


def _prepared(args: argparse.Namespace, out: Path, net: wifi.Profile | None):
    lay = None if args.no_overlay else args.overlay
    layer = net.apply if net else None
    return flash.prepared(args.cache, args.flavor, out, lay, layer)


def _install(args: argparse.Namespace) -> int:
    opts = install.Options(
        args.uboot,
        args.timeout,
        not args.no_backup,
        args.verify_backup,
        args.chunk_ebs,
        wifi_ssid=args.wifi,
    )
    try:
        net = _wifi(args)
        with TemporaryDirectory(prefix="pocketrechip-") as tmp:
            prep = functools.partial(_prepared, args, Path(tmp), net)
            print(install.install(args.cache, prep, opts))
    except (subprocess.SubprocessError, OSError, ValueError, RuntimeError) as e:
        print(
            f"\ninstall stopped: {e}\nFix the cause and run it again; "
            "a backup resumes where it stopped.",
            file=sys.stderr,
        )
        return 1
    return 0


def _flash(args: argparse.Namespace) -> int:
    chunk = args.chunk_mib << 20
    if args.dry_run:
        prep = _prepare(args, args.out)
        m = None if args.no_backup else flash.check_backup(args.backup)
        if args.chip:
            chips = [CHIP_BY_KEY[args.chip]]
        elif m:
            chips = [c for c in NAND_CHIPS.values() if c.name == m["chip"]]
        else:
            chips = list(NAND_CHIPS.values())
        print(flash.dry_run(prep, chips, args.out, chunk), end="")
        if args.wifi:
            print(f"{wifi.describe(args.wifi, args.wifi_open)} added when flashing")
        return 0
    backup_dir = None if args.no_backup else args.backup
    with _prepared(args, args.out, _wifi(args)) as prep:
        chip = flash.flash(_agent(args), args.uboot, prep, backup_dir, chunk)
    print(f"{chip.name}: {DONE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
