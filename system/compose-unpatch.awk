# Remove exactly the two lines winplugd adds to Omarchy's Windows VM compose.
/^[[:space:]]*-[[:space:]]*\/dev\/bus\/usb:\/dev\/bus\/usb[[:space:]]*$/ { next }
/^[[:space:]]*device_cgroup_rules:[[:space:]]*$/ { skip = 1; next }
skip && /^[[:space:]]*-[[:space:]]*"c 189:\* rmw"[[:space:]]*$/ { next }
{ skip = 0; print }
