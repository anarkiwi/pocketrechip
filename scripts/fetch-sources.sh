#!/bin/sh
# Clone or update the upstream repos catalogued in docs/sources.md into cache/src.
set -eu
dest="$(dirname "$0")/../cache/src"
mkdir -p "$dest"
for r in x-chip-os x-chip-linux-deb x-chip-uboot x-chip-deb-repo x-chip-tools \
	x-chip-debootstrap CHIP-dt-overlays pocketchip-configs chip-configs \
	chip-desktop-configs pocketchip-batt chip-power pocketChip-keyboardPatch \
	pocketchip-keypad-patch chip-input-reset CHIP-hwtest; do
	if [ -d "$dest/$r/.git" ]; then
		echo "update $r"
		git -C "$dest/$r" pull -q --ff-only
	else
		echo "clone $r"
		git clone -q "https://github.com/NextThingCo/$r" "$dest/$r"
	fi
done
