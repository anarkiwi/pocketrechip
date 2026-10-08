#!/bin/sh
# Run a pocketrechip device command on an ssh host that has the board on USB.
# The host must see this repo at the same path (shared filesystem). The repo's
# untracked cache/ is mounted at /cache; output goes to cache/<name>.
# flash first builds its images as root (--dry-run), then runs as the user.
# Usage: remote.sh <ssh-host> <name> <probe|backup|flash|restore> [args...]
set -eu
host=$1 name=$2
shift 2
root="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$root/cache/$name"
cache="$(cd "$root/cache" && pwd -P)"
ssh "$host" test -d "$cache/$name" || {
	echo "$host does not see $cache/$name" >&2
	exit 1
}
ssh "$host" docker build -q -t pocketrechip "$root" >/dev/null
docker_run="docker run --rm -t -v $cache:/cache -e POCKETRECHIP_CACHE=/cache"
if [ "$1" = flash ]; then
	ssh -t "$host" "$docker_run" pocketrechip pocketrechip "$@" --out "/cache/$name" --dry-run
	ssh "$host" "$docker_run" pocketrechip chown -R "$(id -u):$(id -g)" \
		/cache/releases /cache/ubifs "/cache/$name"
fi
plugdev=$(ssh "$host" getent group plugdev | cut -d: -f3)
ssh -t "$host" "$docker_run" --privileged --user "$(id -u):$(id -g)" \
	--group-add "$plugdev" -v /dev/bus/usb:/dev/bus/usb \
	pocketrechip pocketrechip "$@" --out "/cache/$name"
