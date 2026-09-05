#!/bin/bash
# Does the panel's connection to the helper survive the helper going away?
#
# Runs a second Quickshell instance (tools/socket-probe.qml, which hosts the
# plugin's HelperLink) against a real winplugd started as a normal user with
# a fake environment, then stops, kills and restarts the helper the way an
# upgrade or a crash would, and checks that the probe reconnects every time
# and gets a fresh state push.  Needs a Wayland session (Quickshell does).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/winplug-reconnect.XXXXXX")
trap 'kill $(jobs -p) 2>/dev/null; sleep 0.2; kill -9 $(jobs -p) 2>/dev/null; rm -rf "$work"' EXIT

fail=0
pass() { printf '  PASS  %s\n' "$*"; }
flunk() { printf '  FAIL  %s\n' "$*"; fail=1; }

command -v quickshell >/dev/null || { echo "quickshell not installed"; exit 2; }
[[ -n ${WAYLAND_DISPLAY:-} ]] || { echo "no Wayland session"; exit 2; }

sock="$work/winplug.sock"
export WINPLUG_SOCKET="$sock" WINPLUG_STATE="$work/state.json" WINPLUG_CONTAINER_PID=2147483647 \
    WINPLUG_NO_UDEV=1 WINPLUG_NO_HOME_FIX=1 WINPLUG_COMPOSE="$work/compose.yml" WINPLUG_SYSFS="$work/sys" \
    WINPLUG_CONF=/nonexistent
mkdir -p "$work/sys"; : >"$work/compose.yml"

daemon_pid=
start_daemon() {
    python3 "$here/../system/winplugd.py" 2>>"$work/daemon.log" &
    daemon_pid=$!
    for i in $(seq 1 50); do [[ -S $sock ]] && return 0; sleep 0.1; done
    return 1
}
# Count of probe events so far, so each step waits for *new* ones.
probe_log="$work/probe.log"
count() { local n; n=$(grep -c "PROBE $1" "$probe_log" 2>/dev/null); echo "${n:-0}"; }
wait_for() {  # wait_for <pattern> <count-above> [tries]
    local n=${3:-60}
    for i in $(seq 1 "$n"); do (( $(count "$1") > $2 )) && return 0; sleep 0.1; done
    return 1
}

# Quickshell only loads QML from inside its config directory: give the probe
# one of its own, with the plugin's real HelperLink.qml beside it.
mkdir -p "$work/probe"
cp "$here/socket-probe.qml" "$here/../HelperLink.qml" "$work/probe/"

echo "1. probe up before the helper exists"
PROBE_SOCKET="$sock" PROBE_RETRY_MS=300 quickshell -p "$work/probe/socket-probe.qml" >"$probe_log" 2>&1 &
wait_for "loaded" 0 100 && pass "probe loaded HelperLink" || { flunk "probe did not load"; cat "$probe_log"; exit 1; }
sleep 1.2
(( $(count "connected=true") == 0 )) && pass "not connected while there is no socket" || flunk "connected to nothing?"

echo "2. helper starts later"
start_daemon || { flunk "daemon did not start"; cat "$work/daemon.log"; exit 1; }
wait_for "connected=true" 0 && pass "connected once the socket appeared" || flunk "never connected: $(tail -n5 "$probe_log")"
wait_for "line" 0 && pass "got the state push" || flunk "no state push"
for i in $(seq 1 30); do grep -qE 'PROBE line \{"type": ?"reply", ?"cmd": ?"ping", ?"ok": ?true' "$probe_log" && break; sleep 0.1; done
grep -qE 'PROBE line \{"type": ?"reply", ?"cmd": ?"ping", ?"ok": ?true' "$probe_log" && pass "and a request got its reply" || flunk "ping unanswered: $(grep 'PROBE line' "$probe_log" | tail -n3)"

echo "3. clean restart (systemctl restart during an upgrade)"
t=$(count "connected=true"); l=$(count "line"); f=$(count "connected=false")
kill -TERM "$daemon_pid"; wait "$daemon_pid" 2>/dev/null
wait_for "connected=false" "$f" && pass "saw the helper go" || flunk "no disconnect seen"
[[ -e $sock ]] && flunk "daemon left its socket behind" || pass "socket removed on exit"
sleep 1  # a few retries against nothing
start_daemon || { flunk "daemon did not restart"; exit 1; }
wait_for "connected=true" "$t" && pass "reconnected after the restart" || flunk "did not reconnect: $(tail -n5 "$probe_log")"
wait_for "line" "$l" && pass "got a fresh state push" || flunk "no state after reconnect"

echo "4. crash: socket file left behind, connections refused until the helper is back"
t=$(count "connected=true"); l=$(count "line"); f=$(count "connected=false")
kill -KILL "$daemon_pid"; wait "$daemon_pid" 2>/dev/null
wait_for "connected=false" "$f" && pass "saw the helper die" || flunk "no disconnect seen"
[[ -S $sock ]] && pass "stale socket file is there (dials will be refused)" || flunk "expected a stale socket file"
sleep 1.5  # several refused dials: this is what wedged the old panel for good
start_daemon || { flunk "daemon did not restart over the stale socket"; cat "$work/daemon.log"; exit 1; }
wait_for "connected=true" "$t" && pass "reconnected after refused dials" || flunk "wedged: $(tail -n5 "$probe_log")"
wait_for "line" "$l" && pass "got a fresh state push" || flunk "no state after reconnect"

echo "5. once more, quickly"
t=$(count "connected=true")
kill -TERM "$daemon_pid"; wait "$daemon_pid" 2>/dev/null
start_daemon || { flunk "daemon did not restart"; exit 1; }
wait_for "connected=true" "$t" && pass "reconnected again" || flunk "did not reconnect the second time"

# QML-level trouble in the link itself.  Not ours: the portal warning (a
# second Quickshell instance), and "not placed in the graphics scene" (the
# probe has no window to put the link in; the bar does).
grep -E 'WARN.*(qml|scene)|PROBE error' "$probe_log" | grep -vE 'qs-blackhole|graphics scene' | head -5 | sed 's/^/  NOTE  /'
# The socket errors above are expected -- one per refused dial -- and are
# what a retry looks like; count them so a silent link shows up.
dials=$(grep -c 'QLocalSocket::' "$probe_log")
(( dials >= 6 )) && pass "$dials failed dials were retried with a fresh socket each" || flunk "only $dials dials seen"

echo
if (( fail )); then echo "FAILED -- probe log:"; cat "$probe_log"; exit 1; else echo "ALL PASSED"; fi
