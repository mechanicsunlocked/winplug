#!/bin/bash
# Winplug -- remove the root half.  Run with sudo.
#
# Devices assigned to Windows are detached first (if it is running).  The USB
# lines the daemon added to the Windows VM compose are removed too, so the VM
# goes back to exactly what Omarchy wrote.
set -euo pipefail

if [ "$(id -u)" != 0 ]; then
    echo "run with sudo" >&2
    exit 1
fi

here=$(cd "$(dirname "$0")" && pwd)
compose=/var/lib/omarchy/windows/docker-compose.yml
if [[ -f /etc/winplug/winplug.conf ]]; then
    c=$(sed -nE 's/^\s*compose\s*=\s*//p' /etc/winplug/winplug.conf | head -1)
    [[ -n $c ]] && compose=$c
fi

# Take every assigned device back first, while the helper can still reach
# QEMU; otherwise they would stay in the guest until Windows restarts.
if systemctl is-active --quiet winplugd.service && [[ -x /usr/local/bin/winplug ]]; then
    keys=$(/usr/local/bin/winplug list --json 2>/dev/null \
        | python3 -c 'import json,sys; print(" ".join(d["key"] for d in json.load(sys.stdin)["devices"] if d.get("assigned")))' 2>/dev/null || true)
    for key in $keys; do
        /usr/local/bin/winplug remove "$key" || true
    done
fi

systemctl disable --now winplugd.service 2>/dev/null || true
rm -f /etc/systemd/system/winplugd.service
systemctl daemon-reload
rm -f /usr/local/bin/winplug
rm -rf /usr/local/lib/winplug /etc/winplug /var/lib/winplug /run/winplug

if [[ -f $compose ]] && grep -q '/dev/bus/usb:/dev/bus/usb' "$compose"; then
    tmp=$(mktemp "$(dirname "$compose")/.compose.XXXXXX")
    awk -f "$here/compose-unpatch.awk" "$compose" >"$tmp"
    chmod --reference="$compose" "$tmp"
    chown --reference="$compose" "$tmp"
    mv -f "$tmp" "$compose"
    echo "USB lines removed from $compose (applies at the next VM start)."
fi
echo "winplug root half removed."
