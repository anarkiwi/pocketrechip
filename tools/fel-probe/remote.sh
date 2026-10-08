#!/bin/sh
# Run a pocketrechip device command on an ssh host that has the board on USB.
# Usage: remote.sh <ssh-host> <remote-out-dir> <probe|backup> [args...]
set -eu
host=$1 out=$2
shift 2
id=$(docker image inspect -f '{{.Id}}' pocketrechip)
[ "$(ssh "$host" docker image inspect -f '{{.Id}}' pocketrechip 2>/dev/null || true)" = "$id" ] ||
	docker save pocketrechip | ssh "$host" docker load
ssh "$host" mkdir -p "$out"
ssh -t "$host" docker run --rm -t --privileged -v /dev/bus/usb:/dev/bus/usb -v "$out:/out" \
	pocketrechip pocketrechip "$@" --out /out
