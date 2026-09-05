#!/bin/bash
# Winplug -- install the user half: the bar widget.
#
# No sudo, nothing outside $HOME.  Idempotent; running it again is how you
# upgrade the widget.  The root half (the helper that talks to QEMU) is one
# separate command, printed at the end, because it is the one thing here that
# needs root and you should see it before you run it.
set -euo pipefail

PLUGIN_ID="io.github.mechanicsunlocked.winplug"
here=$(cd "$(dirname "$0")" && pwd)
plugin_dir="$HOME/.config/omarchy/plugins/$PLUGIN_ID"
warnings=()

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { warnings+=("$*"); printf '    \033[33mwarning:\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ ${1:-} == -h || ${1:-} == --help ]] && { sed -n '2,8p' "$0" | sed 's/^# \?//'; exit 0; }

say "Checking"
command -v omarchy-shell >/dev/null 2>&1 || die "omarchy-shell not found; this needs Omarchy 4."
command -v python3 >/dev/null 2>&1 || die "python3 not found (it is in the official repos: sudo pacman -S python)"
command -v omarchy-windows-vm >/dev/null 2>&1 || warn "omarchy-windows-vm not found; install the Windows VM with: omarchy windows vm install"
note "ok"

say "Installing the bar widget"
if [[ -d $plugin_dir ]] && [[ $here -ef $plugin_dir ]]; then
    note "already in place (added with 'omarchy plugin add')"
else
    mkdir -p "$plugin_dir/system"
    install -Dm644 "$here/manifest.json" "$plugin_dir/manifest.json"
    install -Dm644 "$here/Panel.qml"     "$plugin_dir/Panel.qml"
    install -Dm644 "$here/HelperLink.qml" "$plugin_dir/HelperLink.qml"
    install -Dm755 "$here/system/install.sh"     "$plugin_dir/system/install.sh"
    install -Dm755 "$here/system/uninstall.sh"   "$plugin_dir/system/uninstall.sh"
    install -Dm755 "$here/system/winplugd.py"    "$plugin_dir/system/winplugd.py"
    install -Dm755 "$here/system/winplug"        "$plugin_dir/system/winplug"
    install -Dm644 "$here/system/winplugd.service"     "$plugin_dir/system/winplugd.service"
    install -Dm644 "$here/system/winplug.conf.example" "$plugin_dir/system/winplug.conf.example"
    install -Dm644 "$here/system/compose-unpatch.awk"  "$plugin_dir/system/compose-unpatch.awk"
    note "$plugin_dir"
fi

omarchy-shell shell rescanPlugins >/dev/null 2>&1 || warn "rescanPlugins failed; is the shell running?"
if omarchy-plugin-list --json 2>/dev/null | jq -e --arg id "$PLUGIN_ID" \
        'any(.[]; .id == $id and .enabled == true)' >/dev/null 2>&1; then
    note "already enabled"
else
    omarchy-plugin-enable "$PLUGIN_ID" --section right >/dev/null 2>&1 \
        || omarchy-shell shell setPluginEnabled "$PLUGIN_ID" true >/dev/null 2>&1 \
        || warn "could not enable the plugin; run: omarchy plugin enable $PLUGIN_ID --section right"
    note "enabled, in the right section of the bar"
fi

# "Windows" in the app launcher is Omarchy's desktop entry, which runs
# `omarchy-windows-vm launch`: a password prompt to start, another to stop,
# and an RDP attempt before Windows is up.  Point it at `winplug launch`
# instead, and keep the original line so uninstall.sh can put it back.
say "Wiring the Windows app entry"
entry="$HOME/.local/share/applications/windows-vm.desktop"
if [[ -f $entry ]]; then
    if grep -q '^Exec=.*winplug launch' "$entry"; then
        note "already runs winplug launch"
    else
        orig=$(sed -n 's/^Exec=//p' "$entry" | head -n1)
        tmp=$(mktemp "$entry.XXXXXX")
        awk -v orig="$orig" '
            /^X-Winplug/ { next }
            /^Exec=/ { print "Exec=uwsm app -- /usr/local/bin/winplug launch"; print "X-Winplug-Original-Exec=" orig; next }
            { print }' "$entry" >"$tmp" && mv -f "$tmp" "$entry"
        note "\"Windows\" in the app launcher now runs: winplug launch"
        note "(no password prompts; the session opens once Windows actually answers)"
    fi
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
else
    note "no Windows app entry yet ('omarchy windows vm install' creates it); run this again afterwards"
fi

say "Done"
cat <<EOT

    The USB-on-Windows icon is in the bar.  It will say "Helper not running"
    until the root half is installed, which is this one command:

        sudo $plugin_dir/system/install.sh            # add --autostart to start Windows at boot

    It installs a small service (winplugd) that talks to the VM's QEMU, adds
    USB access to the Windows VM's compose file, and starts and stops Windows
    for you without password prompts.  If Windows is running right now, stop
    and start it once afterwards.  Then: winplug doctor
EOT

if (( ${#warnings[@]} )); then
    printf '\n\033[33m    %d warning(s) above.\033[0m\n' "${#warnings[@]}"
fi
