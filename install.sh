#!/bin/sh
# Install the Linux Terminal Agents Workbench. Start with ./install.sh to see the plan,
# then ./install.sh --install. Run ./install.sh --help for every option.
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
	echo "install.sh: Python 3.11 or newer is needed (sudo apt install python3)." >&2
	exit 1
fi
exec python3 -I -B -c \
	'import sys; sys.path.insert(0, sys.argv.pop(1)); from installer.cli import main; sys.exit(main())' \
	"$here" "$@"
