#!/usr/bin/env bash
# Run a pocketrechip command in its Docker image, on this machine or (--host) on an ssh
# host that sees this repo at the same path. The repo's cache/ is /cache in the container.
# Image builds (install, flash), Wi-Fi installs and qemu-smoke run as root, then hand
# root-owned cache files back; USB steps otherwise run as the invoking user with plugdev.
# Usage: tools/run.sh [--host SSH_HOST] COMMAND [ARGS...]
set -euo pipefail

die() {
	printf '%s: %s\n' "${0##*/}" "$*" >&2
	exit 1
}

host=
if [[ ${1:-} == --host ]]; then
	host=${2:?--host needs an ssh host}
	shift 2
fi
(($#)) || die "usage: $0 [--host SSH_HOST] COMMAND [ARGS...]"
cmd=$1
shift
root=$(cd "$(dirname "$0")/.." && pwd)
where=${host:-this machine}
owner=$(id -u):$(id -g)
tty=()
[[ -t 0 && -t 1 ]] && tty=(-t)

on() {
	if [[ $host ]]; then
		# shellcheck disable=SC2029
		ssh "${ssh_tty[@]}" "$host" "$(printf '%q ' "$@")"
	else
		"$@"
	fi
}
ssh_tty=()

[[ -z $host ]] || ssh "$host" true || die "cannot ssh to $host"
on sh -c 'command -v docker' >/dev/null ||
	die "Docker is not installed on $where: https://docs.docker.com/engine/install/"
on docker info >/dev/null 2>&1 ||
	die "$(id -un) cannot use Docker on $where: sudo usermod -aG docker $(id -un), then log in again"
mkdir -p "$root/cache"
cache=$(cd "$root/cache" && pwd -P)

run=(docker run --rm --init -v "$cache:/cache" -e POCKETRECHIP_CACHE=/cache
	-e "POCKETRECHIP_HOST_CACHE=$cache" -e POCKETRECHIP_WIFI_PASSWORD)
[[ ${tty[*]} ]] && run+=(-it)
args=()
help='' wifi=''
while (($#)); do
	case $1 in
	--wifi-password-file | --wifi-password-file=*)
		if [[ $1 == *=* ]]; then secret=${1#*=}; else secret=${2:?$1 needs a file}; shift; fi
		shift
		secret=$(realpath "$secret")
		on test -r "$secret" || die "$where cannot read $secret"
		run+=(-v "$secret:/run/secrets/wifi-password:ro")
		args+=(--wifi-password-file /run/secrets/wifi-password)
		continue
		;;
	-h | --help) help=1 ;;
	--wifi | --wifi=*) wifi=1 ;;
	esac
	args+=("$1")
	shift
done

ssh_tty=("${tty[@]}")
on docker build -t pocketrechip "$root"

# shellcheck disable=SC2016
host_check='g=$(grep "^plugdev:" /host/etc/group | cut -d: -f3)
r=$(cat /host/etc/udev/rules.d/*.rules /host/usr/lib/udev/rules.d/*.rules 2>/dev/null | grep 1f3a | grep plugdev)
m=; for id in efe8 1010; do echo "$r" | grep -q "$id" || m="$m 1f3a:$id"; done
echo "${g:-none} ${m:-ok}"'
on docker run --rm --mount "type=bind,src=$root,dst=/repo,readonly" \
	--mount "type=bind,src=$cache,dst=/cache,readonly" pocketrechip test -f /repo/install.sh ||
	die "Docker on $where does not see this repo at $root (cache $cache): share it at the same path"

usb=(--privileged -v /dev/bus/usb:/dev/bus/usb)
as_root() {
	# shellcheck disable=SC2016
	local fix='s=0; "$@" || s=$?; find /cache -xdev -user 0 -exec chown -h "$OWNER" {} +; exit $s'
	on "${run[@]}" -e "OWNER=$owner" "$@" pocketrechip sh -c "$fix" sh pocketrechip "$cmd" "${args[@]}"
}
as_user() {
	on "${run[@]}" --user "$owner" "$@" pocketrechip pocketrechip "$cmd" "${args[@]}"
}

case $cmd in
analyze | probe-script) as_user ;;
qemu-smoke) as_root ;;
*)
	if [[ $help ]]; then
		as_user
		exit
	fi
	if [[ $wifi ]]; then
		as_root "${usb[@]}"
		exit
	fi
	ssh_tty=()
	read -r plugdev missing < <(on docker run --rm -v /:/host:ro pocketrechip sh -c "$host_check")
	[[ $plugdev != none ]] || die "no plugdev group on $where: sudo groupadd --system plugdev"
	[[ $missing == ok ]] ||
		die "no udev rule gives plugdev USB access to$missing on $where:" \
			"sudo install -m 644 $root/tools/70-pocketrechip.rules /etc/udev/rules.d/" \
			"&& sudo udevadm control --reload-rules, then reconnect the board"
	ssh_tty=("${tty[@]}")
	if [[ $cmd == install || $cmd == flash ]]; then
		args+=(--prepare-only)
		as_root
		unset 'args[-1]'
	fi
	as_user "${usb[@]}" --group-add "$plugdev"
	;;
esac
