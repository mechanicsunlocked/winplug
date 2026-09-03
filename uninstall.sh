#!/bin/bash
# Winplug -- remove the bar widget.  The root half is printed, not removed:
# taking it out detaches every device from Windows, and that should be a
# decision, not a side effect of tidying up.
set -euo pipefail

PLUGIN_ID="io.github.mechanicsunlocked.winplug"
here=$(cd "$(dirname "$0")" && pwd)
plugin_dir="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

say "Disabling the bar widget"
omarchy-plugin-disable "$PLUGIN_ID" >/dev/null 2>&1 && note "disabled" || note "was not enabled"

if [[ -d $plugin_dir ]] && ! [[ $here -ef $plugin_dir ]]; then
    rm -rf "$plugin_dir"
    note "removed $plugin_dir"
else
    note "leaving $plugin_dir (remove it with: omarchy plugin remove $PLUGIN_ID)"
fi
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true

say "Giving the Windows app entry back to Omarchy"
entry="$HOME/.local/share/applications/windows-vm.desktop"
if [[ -f $entry ]] && grep -q '^X-Winplug-Original-Exec=' "$entry"; then
    orig=$(sed -n 's/^X-Winplug-Original-Exec=//p' "$entry" | head -n1)
    tmp=$(mktemp "$entry.XXXXXX")
    awk -v orig="$orig" '/^X-Winplug/ { next } /^Exec=/ { print "Exec=" orig; next } { print }' "$entry" >"$tmp" && mv -f "$tmp" "$entry"
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    note "restored: $orig"
else
    note "nothing to restore"
fi

say "Done"
cat <<EOT

    The helper service is still running.  To remove it too, and give the
    Windows VM compose back to Omarchy unchanged:

        sudo $here/system/uninstall.sh
EOT
