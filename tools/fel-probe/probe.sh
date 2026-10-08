#!/bin/sh
# Read-only NAND survey of a CHIP/PocketCHIP in FEL mode.
# Usage: probe.sh <ssh-host>   (host has the device on USB and docker)
# U-Boot (built by build-uboot.sh, no on-flash BBT) reads regions into DRAM,
# then serves that DRAM window over USB DFU (read back to cache/fel-probe/dram.bin)
# and resets; with the FEL pin grounded the board comes back up in FEL.
set -eu
host=$1
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$root/cache/fel-probe"
ub="$out/u-boot"
[ -f "$out/u-boot-sunxi-with-spl.bin" ] || "$(dirname "$0")/build-uboot.sh"

EB=0x400000 PAGE=0x4000 NEB=1024
FLAGS=0x44000000 NFC=0x44100000 PAGES=0x45400000 WIN=0x2400000

{
	echo "mw.b $FLAGS 0 0x1000"
	i=0
	for r in "0x44200000 0x800000 0x400000" "0x44600000 0xc00000 0x400000" \
		"0x44a00000 0x1000000 0x800000" "0x45200000 0x100000000 $PAGE" \
		"0x45240000 0x200000000 $PAGE"; do
		set -- $r
		echo "if nand read $1 $2 $3; then mw.b $(printf 0x%x $((FLAGS + i))) 1; else mw.b $(printf 0x%x $((FLAGS + i))) 2; fi"
		i=$((i + 1))
	done
	echo "if nand read.raw 0x45300000 0 1; then mw.b $(printf 0x%x $((FLAGS + i))) 1; else mw.b $(printf 0x%x $((FLAGS + i))) 2; fi"
	n=0
	while [ $n -lt $NEB ]; do
		f=$(printf 0x%x $((FLAGS + 0x100 + n)))
		echo "if nand read $(printf 0x%x $((PAGES + n * PAGE))) $(printf 0x%x $((n * EB))) $PAGE; then mw.b $f 1; else mw.b $f 2; fi"
		n=$((n + 1))
	done
	echo "cp.l 0x1c03000 $NFC 0x40"
	echo "mw.l $(printf 0x%x $((FLAGS + 0xffc))) 0x45464f44"
	echo "setenv dfu_alt_info 'probe ram $FLAGS $WIN'"
	echo "dfu 0 ram 0"
	echo "reset"
} >"$out/probe.cmd"
"$ub/tools/mkimage" -A arm -O linux -T script -C none -n fel-probe \
	-d "$out/probe.cmd" "$out/probe.scr" >/dev/null

rdir=$(ssh "$host" mktemp -d)
scp -q "$out/u-boot-sunxi-with-spl.bin" "$out/probe.scr" "$host:$rdir/"
fel="docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb -v $rdir:/w -w /w debian:trixie sh -c 'apt-get -qq update >/dev/null && apt-get -qq install -y sunxi-tools dfu-util >/dev/null 2>&1 &&"
ssh "$host" "$fel sunxi-fel -p uboot u-boot-sunxi-with-spl.bin write 0x43100000 probe.scr'"
echo "waiting for DFU"
ssh "$host" "for i in \$(seq 180); do lsusb -d 1f3a:1010 >/dev/null && exit 0; sleep 1; done; exit 1"
ssh "$host" "$fel dfu-util -d 1f3a:1010 -a probe -U dram.bin -e'"
scp -q "$host:$rdir/dram.bin" "$out/dram.bin"
ssh "$host" rm -rf "$rdir"
ls -l "$out/dram.bin"
