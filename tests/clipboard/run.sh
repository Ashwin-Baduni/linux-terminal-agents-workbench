#!/bin/bash
# Tests for the Super+V clipboard list, in a throwaway headless GNOME Shell: your desktop, settings
# and real clipboard are never touched. A test-only extension (driver/) drives the list with a
# virtual keyboard and pointer, checks every behaviour and saves screenshots of each state.
# It needs gnome-shell (with --headless), dbus-run-session and glib-compile-schemas.
#
#   tests/clipboard/run.sh [WIDTHxHEIGHT ...]   default: 1280x720 and 1542x868 (two scaled screens)
#   EXT=/path/to/clipboard-list@linux-terminal-agents-workbench tests/clipboard/run.sh
#                                               test another copy instead of this checkout's
#
# About four minutes per size (it copies 200+ items at the speed the list settles them); sizes run
# in parallel. Results and screenshots: the folder printed at the end.
HERE=$(dirname "$(readlink -f "$0")")
UUID=clipboard-list@linux-terminal-agents-workbench
DRIVER=clipboard-list-test@linux-terminal-agents-workbench
EXT=${EXT:-$HERE/../../payload/clipboard/$UUID}
OUT=$(mktemp -d "${TMPDIR:-/tmp}/clipboard-list-tests.XXXXXX")
SIZES=("$@"); [ ${#SIZES[@]} -eq 0 ] && SIZES=(1280x720 1542x868)

one() {
    local size=$1 T=$OUT/$1
    mkdir -p $T/data/gnome-shell/extensions $T/config/glib-2.0/settings $T/out $T/runtime
    # Its own runtime folder: GNOME's start-up crash guard (gnome-shell-disable-extensions) and
    # sockets must never land in the real session's /run/user folder.
    chmod 700 $T/runtime
    cp -r "$EXT" $T/data/gnome-shell/extensions/$UUID
    # The repository ships the schema source only; the installer compiles it, and so does this test.
    glib-compile-schemas $T/data/gnome-shell/extensions/$UUID/schemas
    cp -r $HERE/driver/$DRIVER $T/data/gnome-shell/extensions/
    cat > $T/config/glib-2.0/settings/keyfile <<KEYS
[org/gnome/shell]
enabled-extensions=['$UUID', '$DRIVER']
disable-user-extensions=false

[org/gnome/shell/keybindings]
toggle-message-tray=['<Super>m']

[org/gnome/desktop/interface]
color-scheme='prefer-dark'
KEYS
    (cd $T && exec dbus-run-session -- env XDG_DATA_HOME=$T/data XDG_CONFIG_HOME=$T/config \
        XDG_RUNTIME_DIR=$T/runtime GSETTINGS_BACKEND=keyfile CLIPBOARD_LIST_TEST=1 CLIPBOARD_LIST_OUT=$T/out CLIPBOARD_LIST_MAX_CHARS=600000 \
        gnome-shell --headless --wayland --no-x11 --virtual-monitor $size --wayland-display=wayland-cliptest-$size \
        > $T/shell.log 2>&1) &
    local pid=$!
    for i in $(seq 1 420); do
        grep -q "^DONE\|^ERROR" $T/out/log.txt 2>/dev/null && break
        kill -0 $pid 2>/dev/null || break
        sleep 1
    done
    kill $pid 2>/dev/null; sleep 2
    pkill -f "wayland-display=wayland-cliptest-$size\$" 2>/dev/null
}

for size in "${SIZES[@]}"; do one $size & done; wait
status=0
for size in "${SIZES[@]}"; do
    log=$OUT/$size/out/log.txt
    pass=$(grep -c ^PASS $log 2>/dev/null); fail=$(grep -c ^FAIL $log 2>/dev/null)
    echo "== $size: ${pass:-0} passed, ${fail:-0} failed"
    grep -E "^(FAIL|ERROR)" $log 2>/dev/null
    grep -iE "JS ERROR|TypeError|ReferenceError|Gjs-CRITICAL" $OUT/$size/shell.log | head -5
    grep -q "^DONE failures=0" $log 2>/dev/null || status=1
done
echo "Screenshots and logs: $OUT"
exit $status
