# Remove exactly the two lines winplugd adds to Omarchy's Windows VM compose:
# the /dev/bus/usb bind mount, and the "c 189:* rmw" cgroup rule -- with its
# device_cgroup_rules: header only when no other rule is left under it.
/^[[:space:]]*-[[:space:]]*\/dev\/bus\/usb:\/dev\/bus\/usb[[:space:]]*$/ { next }
/^[[:space:]]*device_cgroup_rules:[[:space:]]*$/ { held = $0; inrules = 1; next }
inrules && /^[[:space:]]*-[[:space:]]*"c 189:\* rmw"[[:space:]]*$/ { next }
inrules && /^[[:space:]]*-/ { if (held != "") { print held; held = "" } print; next }
{ inrules = 0; held = ""; print }
