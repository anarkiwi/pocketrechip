#!/bin/sh
# Read-only health report of a booted PocketCHIP over ssh.
# Usage: device-check.sh [user@]host   (password via SSHPASS with sshpass installed)
set -eu
target=${1:?usage: device-check.sh [user@]host}
ssh_cmd="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
[ -n "${SSHPASS:-}" ] && ssh_cmd="sshpass -e $ssh_cmd -o PubkeyAuthentication=no"
$ssh_cmd "$target" sh -s <<'EOF'
PATH=$PATH:/usr/sbin:/sbin
section() { printf '\n== %s\n' "$1"; }
section system;      uname -r; . /etc/os-release; echo "$PRETTY_NAME"; uptime
section boot;        systemd-analyze 2>/dev/null | head -1; systemctl is-system-running; systemd-analyze blame 2>/dev/null | head -8
section failed;      systemctl --failed --no-legend --plain || true
section memory;      free -m
section swap;        swapon --show --bytes
section zram;        zramctl 2>/dev/null || true
section sysctl;      sysctl vm.page-cluster vm.swappiness
section root;        findmnt -no SOURCE,FSTYPE,OPTIONS /
section journal;     journalctl --header 2>/dev/null | grep -m1 'File path' || true
section timers;      systemctl is-enabled plocate-updatedb.timer 2>&1 || true
section ubi;         for f in /sys/class/ubi/ubi0/*_ebs /sys/class/ubi/ubi0/max_ec /sys/class/ubi/ubi0/bad_peb_count; do [ -r "$f" ] && echo "${f##*/}=$(cat "$f")"; done
section cpufreq;     for f in scaling_governor scaling_cur_freq scaling_available_frequencies cpuinfo_max_freq; do printf '%s=' "$f"; cat /sys/devices/system/cpu/cpu0/cpufreq/$f 2>/dev/null || echo n/a; done
section display;     cat /sys/class/drm/*/status 2>/dev/null | paste -sd' '; ls /sys/class/drm | paste -sd' '
section gpu;         lsmod | grep -E '^(lima|sun4i_drm|panel_simple)' || echo 'lima not loaded'; ls /dev/dri 2>/dev/null | paste -sd' '
section input;       grep -E '^N: Name=' /proc/bus/input/devices | cut -d'"' -f2
section backlight;   for b in /sys/class/backlight/*; do echo "${b##*/} $(cat "$b/brightness")/$(cat "$b/max_brightness")"; done
section audio;       cat /proc/asound/cards
section wifi;        nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device 2>/dev/null; iw dev 2>/dev/null | grep -E 'ssid|channel' || true
section battery;     for p in /sys/class/power_supply/*; do echo "${p##*/}: $(cat "$p/status" 2>/dev/null) $(cat "$p/capacity" 2>/dev/null)%"; done
section cma;         grep -E 'Cma(Total|Free)' /proc/meminfo
section top-rss;     ps -eo rss,comm --sort=-rss | head -8
section kernel-warn; dmesg 2>/dev/null | grep -iE 'error|fail|warn' | grep -vi 'firmware_class' | tail -15 || echo 'dmesg needs root'
EOF
