#!/bin/bash
# Winplug -- install the root half.  Run with sudo, from the plugin directory.
#
#   sudo ~/.config/omarchy/plugins/io.github.mechanicsunlocked.winplug/system/install.sh
#
# Puts the helper daemon and the `winplug` command in place, writes the config
# with the user who ran sudo, and starts the service.  Idempotent; running it
# again is how you upgrade.  The daemon patches the Windows VM compose on its
# first tick, so no other root step exists.
set -euo pipefail

if [ "$(id -u)" != 0 ]; then
    echo "run with sudo" >&2
    exit 1
fi

here=$(cd "$(dirname "$0")" && pwd)
user="${WINPLUG_USER:-${SUDO_USER:-}}"
if [[ -z $user || $user == root ]]; then
    echo "cannot tell which desktop user this is for; run as: sudo $0" >&2
    echo "or set WINPLUG_USER=<name>" >&2
    exit 1
fi
id -u "$user" >/dev/null 2>&1 || { echo "no such user: $user" >&2; exit 1; }

for dep in python3 docker udevadm systemctl; do
    command -v "$dep" >/dev/null 2>&1 || { echo "missing: $dep" >&2; exit 1; }
done

install -Dm755 "$here/winplugd.py" /usr/local/lib/winplug/winplugd.py
install -Dm755 "$here/winplug" /usr/local/bin/winplug
install -Dm644 "$here/winplugd.service" /etc/systemd/system/winplugd.service

install -d -m 0755 /etc/winplug
conf=/etc/winplug/winplug.conf
if [[ -f $conf ]]; then
    # Keep the user's edits; only make sure the user line is right.
    if grep -qE '^\s*user\s*=' "$conf"; then
        sed -i -E "s|^(\s*user\s*=).*|\1 $user|" "$conf"
    else
        sed -i "s|^\[winplug\]|[winplug]\nuser = $user|" "$conf"
    fi
else
    sed "s|CHANGEME|$user|" "$here/winplug.conf.example" >"$conf"
fi
chmod 0644 "$conf"

systemctl daemon-reload
systemctl enable --now winplugd.service
# A restart picks up a new daemon version when this is an upgrade.
systemctl restart winplugd.service

sleep 1
if systemctl is-active --quiet winplugd.service; then
    echo
    echo "winplugd is running for user $user."
    echo "Windows VM compose: the USB bind mount is added automatically; if Windows"
    echo "is running right now, stop and start it once so it gains USB access."
    echo
    echo "Check with:  winplug doctor"
else
    echo "winplugd failed to start:" >&2
    journalctl -u winplugd -n 20 --no-pager >&2 || true
    exit 1
fi
