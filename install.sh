#!/usr/bin/env bash
# Reflash a PocketCHIP in FEL mode on USB with Debian trixie, backing up its NAND first.
# Usage: ./install.sh [--host SSH_HOST] [restore] [OPTIONS...]
set -euo pipefail
host=()
if [[ ${1:-} == --host ]]; then
	host=(--host "${2:?--host needs an ssh host}")
	shift 2
fi
cmd=install
if [[ ${1:-} == restore ]]; then
	cmd=restore
	shift
fi
case " $* " in
*" -h "* | *" --help "*)
	echo "usage: ./install.sh [--host SSH_HOST] [restore] [OPTIONS...]"
	echo "  --host SSH_HOST  the board is on that host's USB; it sees this repo at $(cd "$(dirname "$0")" && pwd)"
	echo "  restore          write this board's backup back instead of installing"
	echo "OPTIONS of pocketrechip $cmd:"
	;;
esac
exec "$(dirname "$0")/tools/run.sh" "${host[@]}" "$cmd" "$@"
