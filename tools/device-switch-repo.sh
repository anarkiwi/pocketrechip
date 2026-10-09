#!/bin/sh
# Switch a booted PocketCHIP from the NextThingCo apt repo to the pocketrechip one,
# upgrade, reboot and report. Usage: device-switch-repo.sh [user@]host
# Password login via SSHPASS (sshpass); sudo password via SUDO_PASSWORD (default: SSHPASS).
# KNOWN_HOSTS pins the host key file.
set -eu
target=${1:?usage: device-switch-repo.sh [user@]host}
here=$(cd "$(dirname "$0")" && pwd)
repo=${REPO_URL:-https://anarkiwi.github.io/x-chip-deb-repo/trixie}
key="$here/../keys/pocketrechip-apt.asc"
ssh_cmd="ssh -o ConnectTimeout=10 -o ServerAliveInterval=15"
[ -n "${KNOWN_HOSTS:-}" ] && ssh_cmd="$ssh_cmd -o UserKnownHostsFile=$KNOWN_HOSTS"
[ -n "${SSHPASS:-}" ] && ssh_cmd="sshpass -e $ssh_cmd -o PubkeyAuthentication=no"
pw=${SUDO_PASSWORD:-${SSHPASS:-}}
run() { $ssh_cmd "$target" "$@"; }
sudo_sh() { { printf '%s\n' "$pw"; cat; } | run "sudo -S -p '' sh -s"; }

echo "== switching apt source to $repo"
{
	cat <<EOF
set -eu
cat > /etc/apt/trusted.gpg.d/chip.key.binary.asc <<'KEY'
$(cat "$key")
KEY
chmod 644 /etc/apt/trusted.gpg.d/chip.key.binary.asc
echo 'deb $repo trixie main' > /etc/apt/sources.list.d/chip.list
export DEBIAN_FRONTEND=noninteractive
apt-get -q update
apt-get -q -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade
dpkg-query -W -f '\${Package} \${Version}\n' linux-image-chip pocketchip-batt
EOF
} | sudo_sh

echo "== rebooting"
printf 'systemctl reboot\n' | sudo_sh || true
sleep 30
i=0
until run true 2>/dev/null; do
	i=$((i + 1))
	[ $i -lt 60 ] || { echo "no ssh after reboot" >&2; exit 1; }
	sleep 5
done
run 'systemctl is-system-running --wait || true'
"$here/device-check.sh" "$target"
