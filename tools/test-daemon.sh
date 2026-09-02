#!/bin/bash
# End-to-end test of winplugd against a fake QEMU monitor, as a normal user.
#
# Exercises: compose patching, assign -> device_add -> attached, unplug ->
# "unplugged" (qdev kept), replug -> attached again, a device QEMU will not
# re-open -> recreated then reported as no-access, detach -> device_del, and a
# stale guest device being cleaned up.  Prints PASS/FAIL per step.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/winplug-test.XXXXXX")
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
ctl plug 1a86:7523 >/dev/null
ctl plug 0403:6001 >/dev/null

export WINPLUG_SOCKET="$work/winplug.sock" WINPLUG_STATE="$work/state.json" \
    WINPLUG_MONITOR="$work/monitor.sock" WINPLUG_CONTAINER_PID=$$ WINPLUG_NO_UDEV=1 \
    WINPLUG_NO_HOME_FIX=1 WINPLUG_COMPOSE="$work/docker-compose.yml" WINPLUG_SYSFS="$work/sys" \
    WINPLUG_CONF=/nonexistent
python3 "$here/../system/winplugd.py" 2>"$work/daemon.log" &
for i in $(seq 1 30); do [[ -S $WINPLUG_SOCKET ]] && break; sleep 0.1; done
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
grep -q '"assigned": {' "$work/state.json" && grep -q '1a86:7523' "$work/state.json" && pass "assignment persisted" || flunk "state not saved"

echo "4. unplug / replug"
ctl unplug 1a86:7523 >/dev/null; rm -rf "$work/sys/3-7"
wait_status 1a86:7523 unplugged && pass "unplug -> 'unplugged'" || flunk "after unplug: $(status_of 1a86:7523)"
dump | grep -q '"winplug-1a86-7523"' && pass "qdev kept for QEMU's own re-attach" || flunk "qdev removed on unplug"
mk 3-7 idVendor=1a86 idProduct=7523 bDeviceClass=ff manufacturer=QinHeng product=USB2.0-Serial busnum=3 devnum=11 speed=12
ctl plug 1a86:7523 >/dev/null
wait_status 1a86:7523 attached && pass "replug -> attached again (new devnum)" || flunk "after replug: $(status_of 1a86:7523)"

echo "5. QEMU fails to re-open the device"
ctl noaccess 1 >/dev/null
wait_status 1a86:7523 attaching 20 && pass "goes to 'attaching' while QEMU is silent" || flunk "state: $(status_of 1a86:7523)"
sleep 8
n=$(dump | python3 -c 'import json,sys; print(sum(1 for c in json.load(sys.stdin)["log"] if c.startswith("device_del")))')
(( n >= 1 )) && pass "recreated the usb-host device after ${REATTACH:-7}s" || flunk "no recreate: $(dump)"
wait_status 1a86:7523 no-access 40 && pass "then reports no-access with a restart hint" || flunk "state: $(status_of 1a86:7523)"
ctl noaccess 0 >/dev/null
wait_status 1a86:7523 attached 40 && pass "recovers when QEMU can open it again" || flunk "state: $(status_of 1a86:7523)"

echo "6. detach"
cli remove 1a86:7523 | grep -q 'on Linux' && pass "remove reports 'on Linux'" || flunk "remove output"
dump | grep -q '"winplug-1a86-7523"' && flunk "qdev still there" || pass "device_del sent"
grep -q '1a86:7523' "$work/state.json" && flunk "still assigned" || pass "assignment cleared"

echo "7. stale guest device"
printf 'device_add usb-host,vendorid=0x0403,productid=0x6001,id=winplug-0403-6001,bus=xhci.0\n' | nc -U -q1 -w2 "$work/monitor.sock" >/dev/null
sleep 3
dump | grep -q '"winplug-0403-6001"' && flunk "stale device left in guest" || pass "unassigned device removed from guest"

echo "8. bad input"
out=$(cli add nonsense 2>&1); grep -qi 'no device' <<<"$out" && pass "unknown device rejected" || flunk "bad input accepted: $out"
printf '{"cmd":"attach","key":"../etc"}\n' | nc -U -q1 -w2 "$WINPLUG_SOCKET" | grep -q '"ok":false' && pass "malformed key rejected" || flunk "malformed key"
printf 'not json\n' | nc -U -q1 -w2 "$WINPLUG_SOCKET" | grep -q 'bad request' && pass "garbage rejected" || flunk "garbage"

echo
if (( fail )); then echo "FAILED -- daemon log:"; cat "$work/daemon.log"; exit 1; else echo "ALL PASSED"; fi
