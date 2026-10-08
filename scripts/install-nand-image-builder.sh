#!/bin/sh
# Build sunxi-tools' sunxi-nand-image-builder (not shipped by Debian's sunxi-tools)
# at the revision Debian trixie packages, and install it into DEST.
# Usage: install-nand-image-builder.sh DEST
set -eu
rev=4390ca668f3b2e62f885edb6952b189c4489d83d
dest=$1
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git clone -q https://github.com/linux-sunxi/sunxi-tools "$src"
git -C "$src" checkout -q "$rev"
make -s -C "$src" sunxi-nand-image-builder
mkdir -p "$dest"
install -m 755 "$src/sunxi-nand-image-builder" "$dest/"
