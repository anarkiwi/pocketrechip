#!/bin/sh
# Verify the published pocketrechip apt repo: InRelease signed by keys/pocketrechip-apt.asc,
# Packages index matching its signed hash, and print package versions.
# Usage: apt-repo-check.sh [PACKAGE[=MIN_VERSION]...]   (exit 1 on any failure)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=${REPO_URL:-https://anarkiwi.github.io/x-chip-deb-repo/trixie}
idx=main/binary-armhf/Packages
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
gpg --batch --quiet --dearmor -o "$tmp/key.gpg" "$here/../keys/pocketrechip-apt.asc"
curl -fsS "$repo/dists/trixie/InRelease" -o "$tmp/InRelease"
gpgv --keyring "$tmp/key.gpg" --output "$tmp/Release" "$tmp/InRelease" 2>"$tmp/gpgv.log" || {
	cat "$tmp/gpgv.log" >&2
	exit 1
}
want=$(awk -v f="$idx" '/^SHA256:/ {s = 1; next} /^[^ ]/ {s = 0} s && $3 == f {print $1}' "$tmp/Release")
[ -n "$want" ] || { echo "$idx not listed in Release" >&2; exit 1; }
curl -fsS "$repo/dists/trixie/$idx" -o "$tmp/Packages"
[ "$(sha256sum "$tmp/Packages" | cut -d' ' -f1)" = "$want" ] || { echo "$idx hash mismatch" >&2; exit 1; }
echo "InRelease signature and $idx hash OK"
for spec in "$@"; do
	pkg=${spec%%=*} min=${spec#*=}
	ver=$(awk -v p="$pkg" '$1 == "Package:" {n = $2} $1 == "Version:" && n == p {print $2}' "$tmp/Packages" | sort -V | tail -1)
	[ -n "$ver" ] || { echo "$pkg: missing" >&2; exit 1; }
	if [ "$min" != "$spec" ] && ! dpkg --compare-versions "$ver" ge "$min"; then
		echo "$pkg $ver < $min" >&2
		exit 1
	fi
	echo "$pkg $ver"
done
