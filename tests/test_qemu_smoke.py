"""QEMU smoke test: probe script, fstab rewrite, parsing, expectations, boot plumbing."""

# pylint: disable=missing-function-docstring

import dataclasses
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile

import pytest

from pocketrechip import cli
from pocketrechip import overlay as O
from pocketrechip import qemu_smoke as Q
from pocketrechip import release as R
from pocketrechip import wifi as W

MEM_KIB, PAGE = 494788, 4096


def show(unit, load="loaded", active="inactive", cond="yes", at=1):
    return (
        f"Id={unit} LoadState={load} ActiveState={active} ConditionResult={cond}"
        f" ConditionTimestampMonotonic={at}"
    )


BOOT_UNITS = {
    **{u: show(u, "masked", cond="yes", at=0) for u in Q.MASKED},
    **{u: show(u, cond="no", at=5000) for u in Q.UPDATES},
    Q.UDISKS: show(Q.UDISKS, at=0),
    Q.NM: show(Q.NM, active="active"),
}
BASE_UNITS = (
    *(show(u, active="active") for u in Q.MASKED),
    show("ldconfig.service", active="active"),
    *(BOOT_UNITS[u] for u in Q.UPDATES[1:]),
    show(Q.UDISKS, active="active"),
    BOOT_UNITS[Q.NM],
)


def boot(*lines, base=tuple(BOOT_UNITS.values()), drop=()):
    """boot_units probe lines: base with lines replacing the same Id, drop removed."""
    ids = {*Q.properties(lines), *drop}
    return {"boot_units": [l for l in base if l.split()[0][3:] not in ids] + [*lines]}


OVERLAY = {
    "state": ["running"],
    "swap": [f"/dev/zram0 partition {Q.zram_swap_bytes(MEM_KIB, PAGE)}"],
    "root_options": ["rw,noatime"],
    "journal_var": ["0"],
    "journal_run": ["1"],
    "plocate_timer": ["masked"],
    "zram_service": ["active"],
    "page_cluster": ["0"],
    "page_size": [str(PAGE)],
    "units": [
        "Id=ubihealthd.service ActiveState=active Result=success NRestarts=7",
        "Id=ssh.service ActiveState=active Result=success NRestarts=0",
        "Id=-.mount ActiveState=active Result=success",
    ],
    "boot_units": list(BOOT_UNITS.values()),
    "analyze": ["Startup finished in 1s (kernel) + 2s (userspace) = 3s"],
    "blame": ["9.000s sshd-keygen.service", "1.000s NetworkManager.service"],
    "udisks_call": ['v s "2.10.1"'],
    "udisks_after": ["active"],
    "free": [
        "               total        used        free",
        f"Mem:          {MEM_KIB}       52012      337568",
        "Swap:         247392           0      247392",
    ],
}
BASELINE = OVERLAY | {
    "state": ["degraded"],
    "swap": [],
    "root_options": ["rw,relatime"],
    "journal_var": ["2"],
    "journal_run": ["0"],
    "plocate_timer": ["enabled"],
    "zram_service": ["inactive"],
    "page_cluster": ["3"],
    "boot_units": list(BASE_UNITS),
}


def serial(sections, end=True):
    """A console log: kernel noise, CRLF endings, probe lines in script order."""
    out = [
        "[    0.000000] Booting Linux\r",
        "\x1b[0;32m  OK  \x1b[0m] Reached target\r",
    ]
    for name, lines in sections.items():
        out += [f"{Q.MARK} {name} {l}\r" for l in lines]
        out.append(f"[   12.3] audit: unit={name}\r")
    if end:
        out.append(f"{Q.MARK} {Q.END}\r")
    return "\n".join(out + ["[  500.0] reboot: Power down", ""])


def test_parse_keeps_probe_lines_and_drops_noise():
    s = Q.parse(serial(OVERLAY))
    assert s["free"] == OVERLAY["free"] and s["units"] == OVERLAY["units"]
    assert s[Q.END] == [""] and "audit" not in s
    assert Q.parse(f"{Q.MARK} blank\r\n{Q.MARK} blank  x\n") == {"blank": ["", " x"]}


def test_result_from_overlay_log():
    r = Q.Result.from_log(serial(OVERLAY))
    assert r.swaps == (("/dev/zram0", "partition", 253329408),)
    assert r.root_options == ("rw", "noatime") and r.mem_total_kib == MEM_KIB
    assert r.failing == ("ubihealthd.service",) and r.page_cluster == 0
    assert r.analyze.startswith("Startup finished")


@pytest.mark.parametrize(
    "sections,end,match",
    [
        (OVERLAY, False, "no end marker"),
        (OVERLAY | {"page_cluster": []}, True, "probe page_cluster"),
        (OVERLAY | {"state": ["running", "degraded"]}, True, "probe state"),
        (OVERLAY | {"free": ["Swap: 1 0 1"]}, True, "no single Mem: line"),
    ],
)
def test_result_rejects_incomplete_logs(sections, end, match):
    with pytest.raises(ValueError, match=match):
        Q.Result.from_log(serial(sections, end))


def test_failing_units():
    assert Q.failing(
        [
            "Id=a.service ActiveState=failed Result=exit-code NRestarts=0",
            "Id=b.service ActiveState=inactive Result=timeout NRestarts=0",
            "Id=c.service ActiveState=activating Result=success NRestarts=2",
            "Id=d.timer ActiveState=active Result=success",
            "Id=e.service ActiveState=active Result=success NRestarts=",
            "garbage",
        ]
    ) == ("a.service", "b.service", "c.service")


def test_zram_swap_bytes_follows_zram_and_mkswap():
    assert Q.zram_swap_bytes(MEM_KIB, PAGE) == 253329408
    assert Q.zram_swap_bytes(8 * 1024, PAGE) == 4 * 1024 * 1024 - PAGE
    assert Q.zram_swap_bytes(8 * 1024 + 2, PAGE) == 4 * 1024 * 1024
    assert Q.zram_swap_bytes(8 * 1024 + 2, 1024) == 4 * 1024 * 1024


@pytest.mark.parametrize("overlay,sections", [(True, OVERLAY), (False, BASELINE)])
def test_evaluate_passes(overlay, sections):
    checks = Q.evaluate(Q.Result.from_log(serial(sections)), overlay)
    assert [c.name for c in checks if not c.ok] == []
    assert len(checks) == 11


BREAKS = {
    "zram swap": [
        {"swap": []},
        {"swap": ["/dev/zram0 partition 253333504"]},
        {"swap": ["/dev/sda2 partition 253329408"]},
        {"swap": ["/dev/zram0 file 253329408"]},
        {"swap": OVERLAY["swap"] * 2},
        {"page_size": ["1024"]},
    ],
    "root noatime": [{"root_options": ["rw,relatime"]}],
    "volatile journal": [{"journal_var": ["1"]}, {"journal_run": ["0"]}],
    "plocate timer": [{"plocate_timer": ["enabled"]}],
    "zram-swap.service": [{"zram_service": ["failed"]}],
    "vm.page-cluster": [{"page_cluster": ["3"]}],
    "masked units": [
        boot(show(Q.MASKED[0], active="active")),
        boot(show(Q.MASKED[-1], "masked", "failed")),
        boot(drop=Q.MASKED[:1]),
    ],
    "udisks2 on demand": [
        boot(show(Q.UDISKS, active="active")),
        {"udisks_call": ["Call failed: Connection timed out"]},
        {"udisks_call": ['v s "2.10.1"', "Call failed: Connection timed out"]},
        {"udisks_call": []},
        {"udisks_after": ["inactive"]},
    ],
    "first-boot updates skipped": [
        boot(show("ldconfig.service", cond="yes")),
        boot(show("systemd-hwdb-update.service", cond="no", at=0)),
    ],
    Q.NM: [boot(show(Q.NM, active="failed")), boot(drop=(Q.NM,))],
    "failing units": [
        {"units": ["Id=zram-swap.service ActiveState=failed Result=exit-code"]}
    ],
}


@pytest.mark.parametrize(
    "name,change", [(n, c) for n, cs in BREAKS.items() for c in cs]
)
def test_evaluate_reports_each_failure(name, change):
    r = Q.Result.from_log(serial(OVERLAY | change))
    assert [c.name for c in Q.evaluate(r, True) if not c.ok] == [name]


@pytest.mark.parametrize(
    "name,change",
    [
        ("zram swap", {"swap": OVERLAY["swap"]}),
        ("root noatime", {"root_options": ["rw,noatime"]}),
        ("volatile journal", {"journal_var": ["0"], "journal_run": ["3"]}),
        ("plocate timer", {"plocate_timer": ["masked"]}),
        ("zram-swap.service", {"zram_service": ["active"]}),
        ("vm.page-cluster", {"page_cluster": ["0"]}),
        ("masked units", boot(*(BOOT_UNITS[u] for u in Q.MASKED), base=BASE_UNITS)),
        ("udisks2 on demand", boot(BOOT_UNITS[Q.UDISKS], base=BASE_UNITS)),
        (
            "udisks2 on demand",
            boot(BOOT_UNITS[Q.UDISKS], base=BASE_UNITS)
            | {"udisks_call": ["Call failed: timed out", 'v s "2.10.1"']},
        ),
        (
            "first-boot updates skipped",
            boot(*(BOOT_UNITS[u] for u in Q.UPDATES), base=BASE_UNITS),
        ),
    ],
)
def test_baseline_flags_overlay_effects(name, change):
    r = Q.Result.from_log(serial(BASELINE | change))
    checks = Q.evaluate(r, False)
    assert [c.name for c in checks if not c.ok] == [name]
    assert next(c for c in checks if c.name == name).expected.startswith("not ")


def test_report_lists_checks_and_expected_failures():
    r = Q.Result.from_log(serial(OVERLAY))
    text = Q.report(r, Q.evaluate(r, True))
    assert text.count("PASS") == 11 and "FAIL" not in text
    assert text.endswith("9.000s sshd-keygen.service\n1.000s NetworkManager.service\n")
    assert "ubihealthd.service is expected to fail: watches /dev/ubi0" in text
    assert "system state: running" in text


def test_fstab_moves_root_to_virtiofs_keeping_options():
    repo = (O.SEARCH[0] / "rootfs/etc/fstab").read_text()
    assert Q.fstab(repo) == "rootfs / virtiofs noatime 0 0\n"
    text = "# / is ubi\nproc /proc proc defaults 0 0\nubi0:rootfs  /  ubifs  ro,noatime  0 1\n"
    assert Q.fstab(text) == (
        "# / is ubi\nproc /proc proc defaults 0 0\nrootfs / virtiofs ro,noatime 0 0\n"
    )
    stub = "# UNCONFIGURED FSTAB FOR BASE SYSTEM\n"
    assert Q.fstab(stub) == stub


@pytest.mark.parametrize(
    "options,kept",
    [
        ("noatime,bulk_read", "noatime"),
        ("ro,no_bulk_read,chk_data_crc,compr=zstd,nodev", "ro,nodev"),
        (
            "bulk_read,auth_key=k,auth_hash_name=sha256,ubi=0,vol=1,assert=panic",
            "defaults",
        ),
        (
            "fast_unmount,norm_unmount,no_chk_data_crc,relatime,x-systemd.a=b",
            "relatime,x-systemd.a=b",
        ),
    ],
)
def test_vfs_options_drop_ubifs_options(options, kept):
    assert Q.vfs_options(options) == kept


def test_device_fstab_has_bulk_read():
    fields = (O.SEARCH[0] / "rootfs/etc/fstab").read_text().split()
    assert fields[:3] == ["ubi0:rootfs", "/", "ubifs"]
    assert set(fields[3].split(",")) == {"noatime", "bulk_read"}


def test_unit_file():
    unit = Q.unit_file().splitlines()
    for line in (
        "After=multi-user.target",
        "WantedBy=multi-user.target",
        "Type=exec",
        f"ExecStart=/bin/sh /{Q.SCRIPT}",
        "StandardOutput=file:/dev/console",
    ):
        assert line in unit
    assert unit.index("[Unit]") < unit.index("[Service]") < unit.index("[Install]")


STUBS = {
    "systemctl": """case "$1" in
  show) printf 'Id=a.service\\nActiveState=failed\\nResult=exit-code\\nNRestarts=0\\n\\n'
        printf 'Id=b.mount\\nActiveState=active\\nResult=success\\n' ;;
  is-system-running) echo running ;;
  is-enabled) echo masked ;;
  is-active) [ "$2" = -q ] || echo active ;;
  poweroff) echo "poweroff $*" >&2 ;;
esac""",
    "dmesg": 'echo "dmesg $*" >&2',
    "swapon": 'echo "/dev/zram0 partition 253329408"',
    "findmnt": "echo rw,noatime",
    "sysctl": "echo 0",
    "getconf": "echo 4096",
    "systemd-analyze": 'echo "Startup finished $*"',
    "busctl": """if [ -e "$0.done" ]; then echo 'v s "2.10.1"'; else
  touch "$0.done"; echo 'Call failed: timed out' >&2; exit 1; fi""",
    "free": f"echo 'total used'; echo 'Mem: {MEM_KIB} 1 2'; echo 'warning' >&2",
}


def test_script_runs_every_probe_through_the_marker(tmp_path):
    for name, body in STUBS.items():
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / name).chmod(0o755)
    sh = tmp_path / "smoke.sh"
    sh.write_text(Q.script())
    env = os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"}
    run = subprocess.run(
        ["sh", str(sh)], env=env, capture_output=True, text=True, check=True
    )
    sections = Q.parse(run.stdout)
    assert set(sections) == {*Q.PROBES, Q.END}
    assert sections["units"] == [
        "Id=a.service ActiveState=failed Result=exit-code NRestarts=0",
        "Id=b.mount ActiveState=active Result=success",
    ]
    assert (
        "warning" in sections["free"]
        and run.stderr == "dmesg -n 1\npoweroff poweroff\n"
    )
    r = Q.Result.from_log(run.stdout)
    assert r.failing == ("a.service",) and r.plocate_timer == "masked"
    assert r.udisks_call == ("Call failed: timed out", 'v s "2.10.1"')
    assert r.udisks_after == "active"
    assert r.blame == ("Startup finished blame",)


def fake_root(tmp_path, kernel_link="boot/vmlinuz-6"):
    root = tmp_path / "root"
    (root / "boot").mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/fstab").write_text("ubi0:rootfs / ubifs noatime 0 0\n")
    (root / "boot/vmlinuz-6").write_bytes(b"k")
    (root / "vmlinuz").symlink_to(kernel_link)
    return root


def test_install_is_idempotent(tmp_path):
    root = fake_root(tmp_path)
    for _ in range(2):
        Q.install(root)
    assert (root / "etc/fstab").read_text() == "rootfs / virtiofs noatime 0 0\n"
    link = root / "etc/systemd/system/multi-user.target.wants" / Q.UNIT
    assert os.readlink(link) == f"/etc/systemd/system/{Q.UNIT}"
    assert (root / "etc/systemd/system" / Q.UNIT).read_text() == Q.unit_file()
    assert (root / Q.SCRIPT).stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("link", ["boot/vmlinuz-6", "/boot/vmlinuz-6"])
def test_in_tree_resolves_links(tmp_path, link):
    root = fake_root(tmp_path, link)
    assert Q.in_tree(root, "vmlinuz") == root / "boot/vmlinuz-6"
    assert Q.in_tree(root, "boot/vmlinuz-6") == root / "boot/vmlinuz-6"


@pytest.mark.parametrize("link", ["../outside", "boot/missing", "boot"])
def test_in_tree_refuses(tmp_path, link):
    (tmp_path / "outside").write_text("x")
    with pytest.raises(FileNotFoundError):
        Q.in_tree(fake_root(tmp_path, link), "vmlinuz")


def test_qemu_argv(tmp_path):
    argv = Q.qemu_argv(tmp_path / "k", tmp_path / "i", tmp_path / "s", 256)
    opt = dict(zip(argv[1::2], argv[2::2]))
    assert argv[0] == "qemu-system-arm" and "-no-reboot" in argv
    assert opt["-M"] == "virt,highmem=off,memory-backend=mem"
    assert opt["-m"] == "256M" and "size=256M,share=on" in opt["-object"]
    assert (opt["-kernel"], opt["-initrd"]) == (
        str(tmp_path / "k"),
        str(tmp_path / "i"),
    )
    assert opt["-chardev"] == f"socket,id=fs,path={tmp_path / 's'}"
    assert opt["-device"] == "vhost-user-fs-pci,chardev=fs,tag=rootfs"
    assert "root=rootfs rootfstype=virtiofs" in opt["-append"]
    assert "console=ttyAMA0" in opt["-append"]
    assert Q.fstab(" x / y noatime 0 0").split()[:3] == ["rootfs", "/", "virtiofs"]
    vfs = Q.virtiofsd_argv(tmp_path, tmp_path / "s")
    assert vfs[vfs.index("--shared-dir") + 1] == str(tmp_path)
    assert vfs[vfs.index("--socket-path") + 1] == str(tmp_path / "s")


def daemon(sock):
    return ["sh", "-c", f"touch {shlex.quote(str(sock))}; exec sleep 30"]


def test_boot_streams_console(tmp_path):
    sock = tmp_path / "sock"
    log = tmp_path / "in.log"
    log.write_text(serial(OVERLAY))
    text = Q.boot(["cat", str(log)], daemon(sock), sock, tmp_path, 10)
    assert text == log.read_text() == (tmp_path / "serial.log").read_text()


def test_boot_failures(tmp_path, monkeypatch):
    sock = tmp_path / "sock"
    with pytest.raises(RuntimeError, match="exited 3"):
        Q.boot(["sh", "-c", "exit 3"], daemon(sock), sock, tmp_path, 10)
    with pytest.raises(TimeoutError, match="still running"):
        Q.boot(["sleep", "30"], daemon(sock), sock, tmp_path, 0.2)
    sock.unlink()
    with pytest.raises(RuntimeError, match="did not create"):
        Q.boot(["true"], ["true"], sock, tmp_path, 10)
    monkeypatch.setattr(Q, "SOCKET_WAIT", 0.2)
    with pytest.raises(RuntimeError, match="did not create"):
        Q.boot(["true"], ["sleep", "30"], sock, tmp_path, 10)


def release_tar(path):
    """A rootfs tar with a kernel, initrd, their / links and the stub fstab."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (
            ("boot/vmlinuz-6", b"k"),
            ("boot/initrd.img-6", b"i"),
            ("etc/fstab", b"# UNCONFIGURED FSTAB FOR BASE SYSTEM\n"),
        ):
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name in ("vmlinuz-6", "initrd.img-6"):
            info = tarfile.TarInfo(f"./{name.split('-', maxsplit=1)[0]}")
            info.type, info.linkname = tarfile.SYMTYPE, f"boot/{name}"
            tar.addfile(info)
    return path


@pytest.fixture(name="fake_vm")
def fake_vm_fixture(tmp_path, monkeypatch):
    """Root faked; QEMU replaced by a script echoing a console log and the tree's fstab."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "lchown", lambda *a: None)
    console = tmp_path / "console.log"

    def qemu(kernel, initrd, sock):
        root = kernel.parents[1]
        assert initrd == root / "boot/initrd.img-6" and sock.parent == root.parent
        fstab = f'sed "s/^/{Q.MARK} fstab /" "{root}/etc/fstab"'
        keys = (
            f'for f in "{root}"/{W.DIR}/*{W.SUFFIX}; do [ -f "$f" ] && '
            f'echo "{Q.MARK} nm_keyfiles $(stat -c %a "$f") root /{W.DIR}/${{f##*/}}"; '
            "done"
        )
        return ["sh", "-c", f'{keys}; cat "{console}"; {fstab}']

    monkeypatch.setattr(Q, "qemu_argv", qemu)
    monkeypatch.setattr(Q, "virtiofsd_argv", lambda root, sock: daemon(sock))
    tar = release_tar(tmp_path / "r.tar.gz")
    asset = R.Asset("os", "t", tar.name, hashlib.sha256(tar.read_bytes()).hexdigest())
    asset.path(tmp_path / "c").parent.mkdir(parents=True)
    tar.rename(asset.path(tmp_path / "c"))
    return asset, console


@pytest.mark.parametrize("overlay,sections", [(True, OVERLAY), (False, BASELINE)])
def test_smoke_end_to_end(tmp_path, fake_vm, overlay, sections):
    asset, console = fake_vm
    console.write_text(serial(sections))
    lay = O.SEARCH[0] if overlay else None
    out = Q.smoke(asset, lay, tmp_path / "c", 10)
    assert out.ok, out.text()
    assert out.logs == tmp_path / "c/qemu-smoke" / Q.smoke_key(asset.sha256, lay)
    fstab = Q.parse((out.logs / "serial.log").read_text())["fstab"]
    assert fstab == (
        ["rootfs / virtiofs noatime 0 0"]
        if overlay
        else ["# UNCONFIGURED FSTAB FOR BASE SYSTEM"]
    )
    saved = json.loads((out.logs / "result.json").read_text())
    assert saved["ok"] and saved["overlay"] == overlay
    assert saved["result"] == json.loads(json.dumps(dataclasses.asdict(out.result)))
    assert ("PASS" in out.text()) and ("no overlay" in out.text()) != overlay


def test_smoke_reports_unfinished_boot(tmp_path, fake_vm):
    asset, console = fake_vm
    console.write_text(serial(OVERLAY, end=False))
    out = Q.smoke(asset, O.SEARCH[0], tmp_path / "c", 10)
    assert not out.ok and out.checks == [] and "no end marker" in out.text()
    assert json.loads((out.logs / "result.json").read_text())["result"] is None


def test_smoke_key_tracks_overlay():
    assert Q.smoke_key("ab" * 32, None) != Q.smoke_key("ab" * 32, O.SEARCH[0])
    assert Q.smoke_key("ab" * 32, None) != Q.smoke_key("cd" * 32, None)


@pytest.mark.parametrize("ok,flags", [(True, []), (False, ["--no-overlay"])])
def test_cli(tmp_path, monkeypatch, capsys, ok, flags):
    seen = {}

    def smoke(asset, overlay, cache, timeout, wifi):
        seen.update(
            asset=asset, overlay=overlay, cache=cache, timeout=timeout, wifi=wifi
        )
        out = Q.Outcome(tmp_path, overlay is not None)
        out.error = "boom"
        if ok:
            out.result = Q.Result.from_log(serial(OVERLAY))
            out.checks = Q.evaluate(out.result, True)
        return out

    monkeypatch.setattr(cli.qemu_smoke, "smoke", smoke)
    argv = ["qemu-smoke", "--cache", str(tmp_path), "--timeout", "5", *flags]
    assert cli.main(argv) == (0 if ok else 1)
    assert seen == {
        "asset": R.ROOTFS["pocketchip"],
        "wifi": None,
        "overlay": None if flags else O.default(),
        "cache": tmp_path,
        "timeout": 5.0,
    }
    assert ("PASS" if ok else "FAIL  boom") in capsys.readouterr().out


@pytest.mark.parametrize("listed", [True, False])
def test_smoke_wifi_checks(tmp_path, fake_vm, monkeypatch, listed):
    monkeypatch.setattr(os, "chown", lambda *a, **kw: None)
    asset, console = fake_vm
    names = ["lo", "Home\\:Net"] if listed else ["lo"]
    console.write_text(serial(OVERLAY | {"nm_connections": names}))
    prof = W.Profile("Home:Net", W.psk("password", "Home:Net"))
    out = Q.smoke(asset, O.SEARCH[0], tmp_path / "c", 10, prof)
    assert out.ok == listed, out.text()
    assert out.result.nm_keyfiles == (f"600 root /{W.DIR}/Home_Net.nmconnection",)
    assert out.logs.name == Q.smoke_key(asset.sha256, O.SEARCH[0], prof.uuid)
    assert [c.ok for c in out.checks[-2:]] == [True, listed]
    assert prof.psk not in (out.logs / "serial.log").read_text()
