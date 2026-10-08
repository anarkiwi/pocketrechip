#!/bin/sh
# Run a pocketrechip device command on an ssh host that has the board on USB.
# The host must see this repo at the same path (shared filesystem); output goes
# to the repo's untracked cache/<name>.
# Usage: remote.sh <ssh-host> <name> <probe|backup> [args...]
set -eu
host=$1 name=$2
shift 2
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$root/cache/$name"
mkdir -p "$out"
out="$(cd "$out" && pwd -P)"
ssh "$host" test -d "$out" || {
	echo "$host does not see $out" >&2
	exit 1
}
ssh "$host" docker build -q -t pocketrechip "$root" >/dev/null
plugdev=$(ssh "$host" getent group plugdev | cut -d: -f3)
ssh -t "$host" docker run --rm -t --privileged --user "$(id -u):$(id -g)" \
	--group-add "$plugdev" -v /dev/bus/usb:/dev/bus/usb -v "$out:/out" \
	pocketrechip pocketrechip "$@" --out /out
