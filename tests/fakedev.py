"""Simulated PocketCHIP for host-flow tests: FEL, the U-Boot agent loop and NAND."""

# pylint: disable=too-many-return-statements

import hashlib
import re
import shlex
import struct
import subprocess
from pathlib import Path

import numpy as np

from pocketrechip import fel_agent as A
from pocketrechip import probe_layout as L
from pocketrechip.backup import Geometry

IMG_MAGIC = 0x27051956
DRAM_LO, DRAM_HI = 0x43000000, 0x58000000
MTDPARTS_RE = re.compile(r"nand0:(0x[0-9a-f]+)@(0x[0-9a-f]+)\(blk\)")
VAR_RE = re.compile(r"\$\{(\w+)\}")
BOOT0_USABLE = 1024
SID = "02c00081:4c4d4e4f:50515253:54555657"


class Reset(Exception):
    """The script restarted the watchdog with reset enabled."""


class Mem:
    """Sparse byte-addressable memory."""

    BLOCK = 1 << 20

    def __init__(self):
        self.blocks: dict[int, np.ndarray] = {}

    def _spans(self, addr: int, n: int):
        while n > 0:
            b, o = divmod(addr, self.BLOCK)
            k = min(n, self.BLOCK - o)
            yield self.blocks.setdefault(b, np.zeros(self.BLOCK, np.uint8)), o, k
            addr, n = addr + k, n - k

    def write(self, addr: int, data) -> None:
        """Store bytes at addr."""
        data = np.frombuffer(bytes(data), np.uint8)
        pos = 0
        for blk, o, k in self._spans(addr, len(data)):
            blk[o : o + k] = data[pos : pos + k]
            pos += k

    def read(self, addr: int, n: int) -> bytes:
        """Load n bytes from addr."""
        return b"".join(blk[o : o + k].tobytes() for blk, o, k in self._spans(addr, n))


class FakeNand:
    """Deterministic NAND contents with bad, uncorrectable and unstable eraseblocks."""

    def __init__(self, geom: Geometry, bad=(), ecc_fail=(), flaky=()):
        self.page, self.pages, self.oob = geom.page, geom.pages, geom.oob
        self.eraseblocks = geom.eraseblocks
        self.bad, self.ecc_fail, self.flaky = set(bad), set(ecc_fail), set(flaky)
        self.reads = 0
        self.erased: set[int] = set()
        self.raw_written: dict[int, bytes] = {}
        self.ecc_written: dict[int, bytes] = {}

    @property
    def eraseblock(self) -> int:
        """Bytes per eraseblock."""
        return self.page * self.pages

    @property
    def size(self) -> int:
        """Chip size."""
        return self.eraseblock * self.eraseblocks

    def orig_raw_page(self, p: int) -> bytes:
        """Factory content of page p as read.raw returns it: data then OOB."""
        return np.random.default_rng([1, p]).bytes(self.page + self.oob)

    def raw_page(self, p: int) -> bytes:
        """Page p as read.raw returns it now."""
        if p in self.raw_written:
            return self.raw_written[p]
        if p in self.ecc_written:
            return self.ecc_written[p] + bytes(self.oob)
        if p // self.pages in self.erased:
            return b"\xff" * (self.page + self.oob)
        return self.orig_raw_page(p)

    def ecc_page(self, p: int) -> bytes:
        """Corrected data of page p; flaky blocks change on every read.

        A raw write reproduces the factory ECC data only with the factory raw bytes.
        """
        self.reads += 1
        if p in self.ecc_written:
            return self.ecc_written[p]
        if p in self.raw_written:
            if self.raw_written[p] != self.orig_raw_page(p):
                return hashlib.sha256(self.raw_written[p]).digest() * (self.page // 32)
        elif p // self.pages in self.erased:
            return b"\xff" * self.page
        salt = self.reads if p // self.pages in self.flaky else 0
        return np.random.default_rng([2, p, salt]).bytes(self.page)

    def erase(self, e: int) -> None:
        """Erase eraseblock e unless it is bad."""
        if e in self.bad:
            return
        self.erased.add(e)
        for p in range(e * self.pages, (e + 1) * self.pages):
            self.raw_written.pop(p, None)
            self.ecc_written.pop(p, None)

    def write_raw(self, off: int, data: bytes) -> None:
        """Raw page writes (data then OOB per page), no bad block check."""
        n = self.page + self.oob
        for i in range(0, len(data), n):
            self.raw_written[off // self.page + i // n] = data[i : i + n]

    def write_ecc(self, off: int, data: bytes) -> bool:
        """nand_write_skip_bad from an eraseblock boundary."""
        e = off // self.eraseblock
        for i in range(0, len(data), self.eraseblock):
            while e in self.bad:
                e += 1
            if e >= self.eraseblocks:
                return False
            block = data[i : i + self.eraseblock]
            for j in range(0, len(block), self.page):
                self.ecc_written[e * self.pages + j // self.page] = block[
                    j : j + self.page
                ]
            e += 1
        return True

    def ecc_eraseblock(self, e: int) -> bytes:
        """Corrected data of eraseblock e."""
        return b"".join(self.ecc_page(e * self.pages + i) for i in range(self.pages))

    def raw_eraseblock(self, e: int) -> bytes:
        """Raw eraseblock e."""
        return b"".join(self.raw_page(e * self.pages + i) for i in range(self.pages))

    def read(self, off: int, size: int, limit: int) -> tuple[bytes, bool]:
        """nand_read_skip_bad: data and success, skipping bad blocks up to limit."""
        eb = self.eraseblock
        blocks, e = [], off // eb
        while len(blocks) * eb < size:
            if e >= self.eraseblocks or (e + 1) * eb - off > limit:
                return b"", False
            if e not in self.bad:
                blocks.append(e)
            e += 1
        first = (off % eb) // self.page
        pages = [b * self.pages + i for b in blocks for i in range(self.pages)]
        data = b"".join(
            map(self.ecc_page, pages[first : first + -(-size // self.page)])
        )
        return data[:size], not self.ecc_fail.intersection(blocks)


class FakeDevice:
    """Runner standing in for mkimage, sunxi-fel and dfu-util against a fake board."""

    def __init__(
        self,
        nand: FakeNand,
        nfc_id: int = 0x40,
        stale: int = 2,
        fail=None,
        inject=None,
    ):
        self.nand, self.stale, self.fail, self.sid = nand, stale, fail, SID
        self.inject = inject or (lambda words: False)
        self.booted: list[str] = []
        self.ubi = UbiModel()
        self.nfc_id = nfc_id
        self.mem = Mem()
        self.wdt_mode = 0
        self.env: dict[str, str] = {}
        self.alts: dict[str, tuple[int, int]] = {}
        self.shown: list[str] = []
        self.pending = 0
        self.state = "fel"
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if self.fail and self.fail(argv, len(self.calls)):
            raise subprocess.CalledProcessError(1, argv)
        return getattr(self, "_" + argv[0].replace("-", "_"))(argv[1:])

    @staticmethod
    def _mkimage(args):
        text = Path(args[args.index("-d") + 1]).read_bytes()
        Path(args[-1]).write_bytes(struct.pack(">II", IMG_MAGIC, len(text)) + text)
        return ""

    @staticmethod
    def _sunxi_nand_image_builder(args):
        """Boot0 image: one page per 1 KiB of input, data then 0xff padding."""
        page = int(args[args.index("-p") + 1]) + int(args[args.index("-o") + 1])
        assert args[:2] == ["-c", "64/1024"] and "-b" in args and "-s" in args
        assert args[args.index("-u") + 1] == str(BOOT0_USABLE)
        data = Path(args[-2]).read_bytes()
        Path(args[-1]).write_bytes(
            b"".join(
                data[i : i + BOOT0_USABLE].ljust(page, b"\xff")
                for i in range(0, len(data), BOOT0_USABLE)
            )
        )
        return ""

    def _sunxi_fel(self, args):
        if args == ["ver"]:
            if self.state != "fel":
                raise subprocess.CalledProcessError(1, ["sunxi-fel", "ver"])
            return "AWUSBFEX soc=00001625(A13)\n"
        if args == ["sid"]:
            assert self.state == "fel"
            return f"{self.sid}\n"
        assert self.state == "fel" and args[:2] == ["-p", "uboot"]
        self.booted.append(args[2])
        assert args[3:5] == ["write", hex(L.SCRIPT_ADDR)]
        text = self._image(Path(args[5]).read_bytes())
        assert text.splitlines() == A.boot_commands()
        self.execute(text.splitlines()[0])
        self._serve()
        return ""

    @staticmethod
    def _image(data: bytes) -> str | None:
        magic, n = struct.unpack(">II", data[:8])
        return data[8 : 8 + n].decode() if magic == IMG_MAGIC else None

    def _serve(self):
        """Enter `dfu 0 ram 0` with the current dfu_alt_info."""
        self.shown = list(self.alts)
        self.alts = {}
        for ent in self.env["dfu_alt_info"].split(";"):
            name, kind, addr, size = ent.split()
            assert kind == "ram"
            self.alts[name] = (int(addr, 16), int(size, 16))
        self.pending = self.stale
        self.state = "dfu"

    def listing(self) -> str:
        """dfu-util -l output, showing the previous session for `stale` polls."""
        if self.state != "dfu":
            return ""
        names = self.shown if self.pending else list(self.alts)
        self.pending = max(0, self.pending - 1)
        return "".join(
            f'Found DFU: [{A.DFU_ID}] ver=0223, devnum=12, cfg=1, intf=0, path="3-2.4", '
            f'alt={i}, name="{n}", serial="UNKNOWN"\n'
            for i, n in enumerate(names)
        )

    def _dfu_util(self, args):
        if args == ["-l"]:
            return self.listing()
        assert args[:2] == ["-d", A.DFU_ID] and self.state == "dfu"
        alt = args[args.index("-a") + 1]
        addr, size = self.alts[alt]
        if "-D" in args:
            data = Path(args[args.index("-D") + 1]).read_bytes()
            assert len(data) <= size
            self.mem.write(addr, data)
        elif "-U" in args:
            path = Path(args[args.index("-U") + 1])
            assert not path.exists()
            path.write_bytes(self.mem.read(addr, size))
        else:
            assert args[-1] == "-e"
            self._loop()
        return ""

    def _loop(self):
        """dfu returned: source the cmd entity, clear its header, serve again."""
        text = self._image(self.mem.read(A.CMD_ADDR, A.CMD_LEN))
        self.mem.write(A.CMD_ADDR, bytes(16))
        try:
            if text is not None:
                self.scripts.append(text)
                for line in text.splitlines():
                    self.execute(line)
        except Reset:
            self.state, self.alts = "fel", {}
            return
        self._serve()

    def execute(self, line: str) -> bool:
        """Run one script line."""
        lex = shlex.shlex(line, posix=True, punctuation_chars=";")
        lex.whitespace_split = True
        stmts, _ = _parse(list(lex), 0, ())
        return self._block(stmts)

    def _block(self, stmts) -> bool:
        ok = True
        for s in stmts:
            if s[0] == "if":
                ok = self._block(s[2] if self._block(s[1]) else s[3])
            else:
                ok = self._cmd(*s)
        return ok

    def store(self, addr: int, data) -> None:
        """Write to DRAM, which must lie inside the agent's usable range."""
        assert DRAM_LO <= addr and addr + len(data) <= DRAM_HI, hex(addr)
        self.mem.write(addr, data)

    def _cmd(self, cmd, *args) -> bool:
        args = tuple(VAR_RE.sub(lambda m: self.env.get(m[1], ""), a) for a in args)
        if self.inject((cmd, *args)):
            return False
        h = [int(a, 16) if re.fullmatch(r"(0x)?[0-9a-f]+", a) else None for a in args]
        h += [None] * 3
        if cmd == "mw.l" and h[0] in (A.WDT_MODE, A.WDT_CTRL):
            self._wdt(h[0], h[1])
        elif cmd in ("mw.b", "mw.l"):
            width = 1 if cmd == "mw.b" else 4
            count = 1 if h[2] is None else h[2]
            self.store(h[0], h[1].to_bytes(width, "little") * count)
        elif cmd == "cp.l":
            self.store(h[1], self.mem.read(h[0], 4 * h[2]))
        elif cmd == "setenv":
            self.env[args[0]] = args[1]
        elif cmd == "itest.b":
            assert args[0].startswith("*") and args[1] == "=="
            return self.mem.read(int(args[0][1:], 16), 1)[0] == h[2]
        elif cmd == "itest":
            assert args[1] == "=="
            return h[0] == h[2]
        elif cmd == "nand":
            return self._nand(args[0], h[1], h[2], args)
        elif cmd == "ubi":
            return self.ubi.command(self, args)
        elif cmd == "ubifsmount":
            return self.ubi.mount(args[0])
        elif cmd == "ubifsload":
            return self.ubi.load(self, h[0], args[1])
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return True

    def _wdt(self, reg: int, value: int) -> None:
        """Watchdog registers: KEY | RESTART with RESET_EN | EN armed resets the SoC."""
        if reg == A.WDT_MODE:
            self.wdt_mode = value
        elif value == A.WDT_CTRL_RESTART and self.wdt_mode == A.WDT_MODE_RESET:
            raise Reset

    def _blk(self) -> tuple[int, int]:
        """(size, offset) of the one-eraseblock `blk` partition."""
        assert self.env["mtdids"] == "nand0=nand0"
        size, off = (
            int(x, 16) for x in MTDPARTS_RE.fullmatch(self.env["mtdparts"]).groups()
        )
        return size, off

    def _nand(self, op, addr, off, args) -> bool:
        """NAND command; the controller holds the ID byte once any NAND command ran."""
        nand = self.nand
        self.mem.write(L.NFC_BASE + L.NFC_ID_BYTE, [self.nfc_id])
        if op == "erase.chip":
            for e in range(nand.eraseblocks):
                nand.erase(e)
            return True
        if op == "erase.part":
            assert args[1] == "blk"
            size, off = self._blk()
            assert size == nand.eraseblock
            nand.erase(off // nand.eraseblock)
            return True
        if op == "write.raw.noverify":
            count = int(args[3], 16)
            if off + count * nand.page > nand.size:
                return False
            nand.write_raw(off, self.mem.read(addr, count * (nand.page + nand.oob)))
            return True
        if op == "write":
            size = int(args[3], 16)
            assert off % nand.eraseblock == 0
            return nand.write_ecc(off, self.mem.read(addr, size))
        if op == "read.raw":
            count = int(args[3], 16)
            if off + count * nand.page > nand.size:
                return False
            p0 = off // nand.page
            self.store(addr, b"".join(nand.raw_page(p0 + i) for i in range(count)))
            return True
        assert op == "read"
        size = int(args[3], 16)
        if off is None:
            assert args[2] == "blk"
            psize, off = self._blk()
            limit = psize
            assert size <= psize
        else:
            if off >= nand.size:
                return False
            limit = nand.size - off
        data, ok = nand.read(off, size, limit)
        self.store(addr, data)
        return ok


class UbiModel:
    """`ubi part/createvol/write.part`, `ubifsmount`, `ubifsload` on a byte volume.

    `filesystems` maps the sha256 of a UBIFS image to its files; mounting needs a
    complete update whose bytes are a registered image.
    """

    def __init__(self, capacity: int = 1 << 30):
        self.capacity = capacity
        self.attached = False
        self.volume: bytearray | None = None
        self.total = 0
        self.filesystems: dict[str, dict[str, bytes]] = {}
        self.mounted: dict[str, bytes] | None = None
        self.calls: list[tuple[int, int | None]] = []

    def register(self, image: bytes, files: dict[str, bytes]) -> None:
        """Files a UBIFS image holds."""
        self.filesystems[hashlib.sha256(image).hexdigest()] = files

    def command(self, dev: "FakeDevice", args) -> bool:
        """ubi subcommands."""
        if args[0] == "part":
            self.attached = args[1] == "rootfs"
            return self.attached
        if args[0] == "createvol":
            assert len(args) == 2
            if not self.attached or args[1] != "rootfs":
                return False
            self.volume, self.total = bytearray(), 0
            return True
        assert args[0] == "write.part" and args[2] == "rootfs"
        if self.volume is None:
            return False
        addr, size = int(args[1], 16), int(args[3], 16)
        full = int(args[4], 16) if len(args) > 4 else None
        self.calls.append((size, full))
        if full is not None:
            if size > self.capacity:
                return False
            self.volume, self.total = bytearray(), full
        elif len(self.volume) >= self.total:
            return False
        self.volume += dev.mem.read(addr, min(size, self.total - len(self.volume)))
        return True

    def mount(self, name: str) -> bool:
        """Mount a completely written, registered volume."""
        complete = self.volume is not None and len(self.volume) == self.total
        digest = hashlib.sha256(self.volume or b"").hexdigest()
        self.mounted = self.filesystems.get(digest) if complete else None
        return name == "ubi0:rootfs" and self.mounted is not None

    def load(self, dev: "FakeDevice", addr: int, path: str) -> bool:
        """Load a file of the mounted volume; sets filesize."""
        if self.mounted is None or path not in self.mounted:
            return False
        dev.store(addr, self.mounted[path])
        dev.env["filesize"] = f"{len(self.mounted[path]):x}"
        return True


def _parse(toks, i, stops):
    """Statements up to a stop keyword: word lists or ('if', cond, then, else)."""
    out = []
    while i < len(toks) and toks[i] not in stops:
        if toks[i] == ";":
            i += 1
        elif toks[i] == "if":
            cond, i = _parse(toks, i + 1, ("then",))
            then, i = _parse(toks, i + 1, ("else", "fi"))
            other = []
            if toks[i] == "else":
                other, i = _parse(toks, i + 1, ("fi",))
            out.append(("if", cond, then, other))
            i += 1
        else:
            j = i
            while j < len(toks) and toks[j] != ";":
                j += 1
            out.append(toks[i:j])
            i = j
    return out, i
