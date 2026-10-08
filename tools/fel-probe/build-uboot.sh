#!/bin/sh
# Build x-chip-uboot with the on-flash bad block table disabled, so NAND access
# only reads OOB markers and never writes a BBT. Output: cache/fel-probe/u-boot-sunxi-with-spl.bin
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
src="$root/cache/src/x-chip-uboot"
out="$root/cache/fel-probe"
[ -d "$src" ] || "$root/scripts/fetch-sources.sh"
mkdir -p "$out"
docker build -q -t chip-uboot-amd64 "$src" >/dev/null
docker run --rm -v "$src:/src:ro" -v "$out:/out" -w /out \
	-e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" chip-uboot-amd64 sh -euxc '
	rm -rf u-boot
	git clone -q --depth 1 --branch v2022.01 https://github.com/u-boot/u-boot
	cd u-boot
	for p in sunxi-Add-support-for-slc-emulation-on-mlc-NAND \
		sunxi-nand-Undo-removal-of-DMA-specific-code-that-br \
		cmd-w1-read-add-optional-dest-address; do
		git apply /src/0001-$p.patch
	done
	grep -v USE_FLASH_BBT /src/nand.cfg >> configs/CHIP_defconfig
	echo "# CONFIG_SYS_NAND_USE_FLASH_BBT is not set" >> configs/CHIP_defconfig
	export ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf-
	make -s CHIP_defconfig
	make -s -j"$(nproc)"
	! grep -q "^CONFIG_SYS_NAND_USE_FLASH_BBT=y" .config
	cp u-boot-sunxi-with-spl.bin ..
	chown -R "$HOST_UID:$HOST_GID" /out'
ls -l "$out/u-boot-sunxi-with-spl.bin"
