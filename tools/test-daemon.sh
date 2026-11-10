#!/bin/bash
# End-to-end test of winplugd against a fake QEMU monitor, as a normal user.
#
# Exercises: compose patching, assign -> device_add by node -> attached,
# unplug -> "unplugged" (qdev kept), replug at a new node -> the empty qdev is
# swapped for the new device, a node QEMU may not open -> no-access, a helper
# restart with a device in Windows, detach -> device_del, a stale guest device
# being cleaned up, and the vendor:product fallback for a QEMU without
# hostdevice.  Prints PASS/FAIL per step.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/winplug-test.XXXXXX")
# Nothing may be written into the plugin directory (Omarchy reloads the
# plugin on any change there, which strands a locked screen), and the
# deliberate failures below must not pop up as desktop notifications.
export PYTHONDONTWRITEBYTECODE=1 WINPLUG_NO_NOTIFY=1
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$work"' EXIT

fail=0
pass() { printf '  PASS  %s\n' "$*"; }
flunk() { printf '  FAIL  %s\n' "$*"; fail=1; }
ctl() { printf '%s\n' "$*" | nc -U -q1 -w2 "$work/ctl.sock"; }
dump() { ctl dump; }

# A fake sysfs with one dongle and one hub.
mk() { local d="$work/sys/$1"; shift; mkdir -p "$d"; while (($#)); do printf '%s\n' "${1#*=}" >"$d/${1%%=*}"; shift; done; }
mk usb3 idVendor=1d6b idProduct=0003 bDeviceClass=09
mk 3-7 idVendor=1a86 idProduct=7523 bDeviceClass=ff manufacturer=QinHeng product=USB2.0-Serial busnum=3 devnum=9 speed=12
mk 3-8 idVendor=0403 idProduct=6001 bDeviceClass=00 manufacturer=FTDI product="FT232R USB UART" busnum=3 devnum=10 speed=12

# Omarchy's compose, before winplug touches it.
cat >"$work/docker-compose.yml" <<'YML'
services:
  windows:
    image: dockurr/windows
    container_name: omarchy-windows
    environment:
      PROTECT: "Y"
    devices:
      - /dev/kvm
    volumes:
      - /var/lib/omarchy/windows/mounts/users/1000/storage:/storage
      - /var/lib/omarchy/windows/mounts/users/1000/shared:/shared
    restart: "no"
YML
chmod 0640 "$work/docker-compose.yml"

python3 "$here/fake-hmp.py" "$work/monitor.sock" "$work/ctl.sock" &
sleep 0.3
ctl plug 1a86:7523 /dev/bus/usb/003/009 >/dev/null
ctl plug 0403:6001 /dev/bus/usb/003/010 >/dev/null
# From here on the fake QEMU is as blind as the real one in its container:
# matching by vendor:product finds nothing that is plugged in later.
ctl stale 1 >/dev/null

export WINPLUG_SOCKET="$work/winplug.sock" WINPLUG_STATE="$work/state.json" \
    WINPLUG_MONITOR="$work/monitor.sock" WINPLUG_CONTAINER_PID=$$ WINPLUG_NO_UDEV=1 \
    WINPLUG_NO_HOME_FIX=1 WINPLUG_COMPOSE="$work/docker-compose.yml" WINPLUG_SYSFS="$work/sys" \
    WINPLUG_CONF=/nonexistent WINPLUG_AUDIT_SECONDS=4
start_daemon() {
    python3 "$here/../system/winplugd.py" 2>>"$work/daemon.log" &
    daemon_pid=$!
    for i in $(seq 1 30); do [[ -S $WINPLUG_SOCKET ]] && break; sleep 0.1; done
}
start_daemon
[[ -S $WINPLUG_SOCKET ]] && pass "daemon came up" || { flunk "daemon did not start"; cat "$work/daemon.log"; exit 1; }
sleep 0.5

cli() { python3 "$here/../system/winplug" "$@"; }
status_of() { cli list --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next((d["status"] for d in s["devices"] if d["key"]==sys.argv[1]), "-"))' "$1"; }
wait_status() { local key=$1 want=$2 n=${3:-30}; for i in $(seq 1 "$n"); do [[ $(status_of "$key") == "$want" ]] && return 0; sleep 0.25; done; return 1; }

echo "1. compose"
grep -q '/dev/bus/usb:/dev/bus/usb' "$work/docker-compose.yml" && grep -q 'c 189:\* rmw' "$work/docker-compose.yml" \
    && pass "compose gained the USB bind mount and cgroup rule" || flunk "compose not patched"
[[ $(stat -c %a "$work/docker-compose.yml") == 640 ]] && pass "compose mode kept at 0640" || flunk "compose mode changed"
[[ $(grep -c ':/storage$' "$work/docker-compose.yml") == 1 ]] && pass "storage mount untouched" || flunk "storage mount duplicated"

echo "2. list"
cli list | grep -q '1a86:7523' && pass "CLI lists the dongle" || flunk "CLI list"
cli list | grep -q '1d6b:0003' && flunk "hub listed" || pass "hub hidden"
cli status | grep -q 'Windows is running' && pass "VM reported running" || flunk "VM status: $(cli status)"

echo "3. attach"
cli add 1a86:7523 | grep -q 'in Windows' && pass "add reports 'in Windows'" || flunk "add: $(cli add 1a86:7523)"
dump | grep -q '"winplug-1a86-7523"' && pass "QEMU got device_add" || flunk "no qdev: $(dump)"
dump | grep -q 'bus=xhci.0' && pass "attached on xhci.0" || flunk "not on xhci: $(dump)"
qdev() { dump | python3 -c 'import json,sys; d=json.load(sys.stdin)["qdevs"].get(sys.argv[1]); print("%s %s" % (d["node"] or "by-id", "open" if d["open"] else "empty") if d else "none")' "$1"; }
[[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/009 open" ]] && pass "added by its device node" || flunk "qdev: $(qdev winplug-1a86-7523)"
grep -q '"assigned": {' "$work/state.json" && grep -q '1a86:7523' "$work/state.json" && pass "assignment persisted" || flunk "state not saved"

echo "4. unplug / replug"
ctl unplug 1a86:7523 >/dev/null; rm -rf "$work/sys/3-7"
wait_status 1a86:7523 unplugged && pass "unplug -> 'unplugged'" || flunk "after unplug: $(status_of 1a86:7523)"
[[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/009 empty" ]] && pass "qdev kept while unplugged (never removed on a guess)" || flunk "qdev: $(qdev winplug-1a86-7523)"
del_count() { dump | python3 -c 'import json,sys; print(json.load(sys.stdin)["total_del"])'; }
replug() {  # replug <devnum>: the dongle comes back at a new address, as it always does
    mk 3-7 idVendor=1a86 idProduct=7523 bDeviceClass=ff manufacturer=QinHeng product=USB2.0-Serial busnum=3 devnum="$1" speed=12
    ctl plug 1a86:7523 "/dev/bus/usb/003/0$1" >/dev/null
}
before=$(del_count)
replug 11
for i in $(seq 1 40); do [[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/011 open" ]] && break; sleep 0.25; done
[[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/011 open" ]] && pass "replug -> QEMU holds the device at its new node" || flunk "qdev after replug: $(qdev winplug-1a86-7523)"
(( $(del_count) == before + 1 )) && pass "by swapping the empty qdev, once" || flunk "device_del count went $before -> $(del_count)"
wait_status 1a86:7523 attached && pass "and it reports 'in Windows' again" || flunk "after replug: $(status_of 1a86:7523)"
before=$(del_count); sleep 5
(( $(del_count) == before )) && [[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/011 open" ]] && pass "a device in Windows is left alone" || flunk "touched an attached device: $(dump)"

echo "5. QEMU may not open the replugged device (VM started without the USB rule)"
ctl unplug 1a86:7523 >/dev/null; rm -rf "$work/sys/3-7"
wait_status 1a86:7523 unplugged || flunk "after unplug: $(status_of 1a86:7523)"
ctl noaccess 1 >/dev/null
replug 12
wait_status 1a86:7523 no-access 40 && pass "reports no-access with a restart hint" || flunk "state: $(status_of 1a86:7523)"
cli list --json | grep -q 'restart Windows' && pass "and says to restart Windows" || flunk "no hint"
ctl noaccess 0 >/dev/null
wait_status 1a86:7523 attached 80 && pass "recovers when QEMU can open it again" || flunk "state: $(status_of 1a86:7523)"

echo "5b. the helper is restarted while a device is in Windows (an upgrade)"
before=$(del_count)
kill "$daemon_pid"; wait "$daemon_pid" 2>/dev/null; rm -f "$WINPLUG_SOCKET"
start_daemon
wait_status 1a86:7523 attached 40 && pass "still 'in Windows' after the restart" || flunk "state: $(status_of 1a86:7523)"
sleep 3
(( $(del_count) == before )) && [[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/012 open" ]] && pass "and the device was not touched" || flunk "restart disturbed the device: $(dump)"
ctl unplug 1a86:7523 >/dev/null; rm -rf "$work/sys/3-7"
wait_status 1a86:7523 unplugged || flunk "after unplug: $(status_of 1a86:7523)"
replug 13
for i in $(seq 1 40); do [[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/013 open" ]] && break; sleep 0.25; done
[[ $(qdev winplug-1a86-7523) == "/dev/bus/usb/003/013 open" ]] && pass "a replug after the restart is still followed (QEMU is asked for the old node)" || flunk "qdev: $(qdev winplug-1a86-7523)"

echo "6. detach"
cli remove 1a86:7523 | grep -q 'on Linux' && pass "remove reports 'on Linux'" || flunk "remove output"
dump | grep -q '"winplug-1a86-7523"' && flunk "qdev still there" || pass "device_del sent"
grep -q '1a86:7523' "$work/state.json" && flunk "still assigned" || pass "assignment cleared"

echo "7. stale guest device"
printf 'device_add usb-host,vendorid=0x0403,productid=0x6001,id=winplug-0403-6001,bus=xhci.0\n' | nc -U -q1 -w2 "$work/monitor.sock" >/dev/null
# Nothing is assigned now, so only the slow audit (4s in this test) looks.
for i in $(seq 1 40); do dump | grep -q '"winplug-0403-6001"' || break; sleep 0.25; done
dump | grep -q '"winplug-0403-6001"' && flunk "stale device left in guest" || pass "unassigned device removed from guest by the audit"

echo "8. the monitor is left alone when there is nothing to do"
polls() { dump | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'; }
# Nothing assigned: only the slow audit runs (4s in this test).
a=$(polls); sleep 6.5; b=$(polls)
(( b - a <= 3 )) && pass "$((b - a)) monitor commands in 6.5s with nothing assigned (audit only)" || flunk "still polling with nothing assigned: $((b - a)) in 6.5s"
# One device attached and steady: a light heartbeat, not the tight loop.
cli add 0403:6001 >/dev/null
wait_status 0403:6001 attached 40 || flunk "0403 did not attach"
sleep 0.5  # let the confirming poll settle
a=$(polls); sleep 6.5; b=$(polls)
(( b - a <= 3 )) && pass "$((b - a)) commands in 6.5s with a device attached and idle (${ATTACHED:-5}s heartbeat)" || flunk "attached device polled too often: $((b - a)) in 6.5s"
cli remove 0403:6001 >/dev/null

echo "8b. QEMU refuses a device outright: one warning, a slow retry, no command storm"
add_count() { dump | python3 -c 'import json,sys; print(json.load(sys.stdin)["total_add"])'; }
ctl refuse_add 1 >/dev/null
cli add 0403:6001 >/dev/null
wait_status 0403:6001 error 40 && pass "refused device shows 'error'" || flunk "state: $(status_of 0403:6001)"
a=$(add_count); sleep 6.5; b=$(add_count)
(( b - a <= 1 )) && pass "$((b - a)) device_add in 6.5s while refused (retry every ${ADD_RETRY:-10}s)" || flunk "device_add storm: $((b - a)) in 6.5s"
(( $(grep -c 'device_add 0403:6001 failed' "$work/daemon.log") == 1 )) && pass "warned once" || flunk "warned $(grep -c 'device_add 0403:6001 failed' "$work/daemon.log") times"
ctl refuse_add 0 >/dev/null
wait_status 0403:6001 attached 80 && pass "attaches once QEMU accepts it" || flunk "state: $(status_of 0403:6001)"
cli remove 0403:6001 >/dev/null

echo "8c. a QEMU without the hostdevice property: fall back to vendor:product"
ctl stale 0 >/dev/null; ctl unplug 0403:6001 >/dev/null; ctl plug 0403:6001 /dev/bus/usb/003/010 >/dev/null
ctl nohostdevice 1 >/dev/null
cli add 0403:6001 >/dev/null
wait_status 0403:6001 attached 40 && pass "attached" || flunk "state: $(status_of 0403:6001)"
[[ $(qdev winplug-0403-6001) == "by-id open" ]] && pass "by vendor:product" || flunk "qdev: $(qdev winplug-0403-6001)"
(( $(grep -c 'no usb-host hostdevice property' "$work/daemon.log") == 1 )) && pass "said so once" || flunk "warned $(grep -c 'no usb-host hostdevice property' "$work/daemon.log") times"
cli remove 0403:6001 >/dev/null

echo "9. bad input"
out=$(cli add nonsense 2>&1); grep -qi 'no device' <<<"$out" && pass "unknown device rejected" || flunk "bad input accepted: $out"
printf '{"cmd":"attach","key":"../etc"}\n' | nc -U -q1 -w2 "$WINPLUG_SOCKET" | grep -q '"ok":false' && pass "malformed key rejected" || flunk "malformed key"
printf 'not json\n' | nc -U -q1 -w2 "$WINPLUG_SOCKET" | grep -q 'bad request' && pass "garbage rejected" || flunk "garbage"

echo "10. starting and stopping Windows through Omarchy's launcher (a fake one)"
# A second helper whose container does not exist, a fake omarchy-windows-vm
# beside a log, autostart on: it must run `__priv up` for the configured user
# by itself, stop on request, report launcher errors, and flip autostart in
# its config file.
cp "$here/fake-vm-tool.sh" "$work/omarchy-windows-vm"; chmod +x "$work/omarchy-windows-vm"
mkdir -p "$work/run2"
(
    export WINPLUG_SOCKET="$work/run2/winplug.sock" WINPLUG_STATE="$work/state2.json" \
        WINPLUG_CONTAINER_PID=2147483647 WINPLUG_CONF="$work/winplug2.conf" WINPLUG_USER="$USER" \
        WINPLUG_VM_TOOL="$work/omarchy-windows-vm" WINPLUG_TRUST_VM_TOOL=1 WINPLUG_VM_RUNNER=direct \
        WINPLUG_AUTOSTART=1 WINPLUG_AUTOSTART_DELAY=1 WINPLUG_BOOT_AGE=30
    exec python3 "$here/../system/winplugd.py" 2>"$work/daemon2.log"
) &
for i in $(seq 1 30); do [[ -S $work/run2/winplug.sock ]] && break; sleep 0.1; done
cli2() { WINPLUG_SOCKET="$work/run2/winplug.sock" python3 "$here/../system/winplug" "$@"; }
for i in $(seq 1 40); do grep -q 'argv: __priv up' "$work/fake-vm.log" 2>/dev/null && break; sleep 0.25; done
grep -q 'argv: __priv up' "$work/fake-vm.log" 2>/dev/null && pass "autostart ran 'omarchy-windows-vm __priv up'" || flunk "no autostart call: $(cat "$work/fake-vm.log" 2>/dev/null)"
grep -q "env: PKEXEC_UID=$(id -u) HOME=/root LC_ALL=C.UTF-8 PATH=/usr/local/sbin" "$work/fake-vm.log" && pass "for the configured user, with a clean environment" || flunk "environment: $(grep env: "$work/fake-vm.log")"
[[ -f $work/run2/autostarted ]] && pass "autostart marker written, so it runs once per boot" || flunk "no autostart marker"
cli2 status | grep -q 'autostart on' && pass "status shows autostart" || flunk "status: $(cli2 status)"
cli2 vm stop | grep -q 'Windows stopped' && pass "winplug vm stop" || flunk "vm stop: $(cli2 vm stop 2>&1)"
grep -q 'argv: __priv down' "$work/fake-vm.log" && pass "stop ran '__priv down'" || flunk "no down call"
cli2 autostart off | grep -q 'Autostart off' && pass "winplug autostart off" || flunk "autostart off"
grep -q '^autostart = false' "$work/winplug2.conf" && pass "config file updated" || flunk "config: $(cat "$work/winplug2.conf" 2>&1)"
cli2 autostart on >/dev/null && grep -q '^autostart = true' "$work/winplug2.conf" && pass "and back on" || flunk "autostart on"
touch "$work/fake-vm.fail"
out=$(cli2 vm start 2>&1); grep -q 'fake: cannot up' <<<"$out" && pass "launcher errors reach the user: '$(tail -n1 <<<"$out")'" || flunk "error not reported: $out"
rm -f "$work/fake-vm.fail"
out=$(cli2 vm start 2>&1); grep -q 'Windows is starting' <<<"$out" && pass "winplug vm start" || flunk "vm start: $out"

echo
if (( fail )); then echo "FAILED -- daemon log:"; cat "$work/daemon.log"; exit 1; else echo "ALL PASSED"; fi
