<p align="center"><img src="logo.svg" width="96" alt="Winplug"></p>

# Winplug

**USB devices into the Omarchy Windows VM, from the bar. Hot-plug safe.**

Plug in the dongle, the car interface, the bike tool. Open the USB-on-Windows
icon in the top bar, click the device, and it is in Windows. Click again and it
is back on Linux. It stays assigned across replugs and across Windows restarts
until you take it back, and it goes through QEMU's own USB passthrough, so the
device talks to Windows the way it would on real hardware: no RDP redirection,
no USB/IP, no extra software in the guest.

Built for Omarchy 4's Windows VM (`omarchy windows vm`, which is
[dockur/windows](https://github.com/dockur/windows) under the hood).

---

## Install

```bash
omarchy plugin add https://github.com/mechanicsunlocked/winplug.git --enable --yes
~/.config/omarchy/plugins/io.github.mechanicsunlocked.winplug/install.sh
sudo ~/.config/omarchy/plugins/io.github.mechanicsunlocked.winplug/system/install.sh
```

The first two lines put the widget in the bar, no root. The third installs
`winplugd`, a small root service, and the `winplug` command. Root is needed
because the VM's QEMU lives inside a root-owned container and Omarchy keeps
you out of the docker group on purpose; the service is the narrow bridge
across that, and it accepts exactly four commands (list, send, take back,
check compose) from exactly one user.

If Windows is running when you install, **stop it and start it once**. The
service adds USB access to the VM's compose file, and that only takes effect
at the next start. From then on nothing ever needs a restart.

Check the whole chain with:

```bash
winplug doctor
```

Running the same three lines again is how you upgrade.

### Removing it

```bash
~/.config/omarchy/plugins/io.github.mechanicsunlocked.winplug/uninstall.sh
sudo ~/.config/omarchy/plugins/io.github.mechanicsunlocked.winplug/system/uninstall.sh
omarchy plugin remove io.github.mechanicsunlocked.winplug
```

The root uninstall also takes the two USB lines back out of the compose file,
so the Windows VM is exactly what Omarchy wrote.

---

## How to use it

Click the icon. Two lists: **In Windows** and **On this machine**.

| Do this | Get this |
|---|---|
| click a device on this machine | it goes to Windows |
| click a device in Windows | it comes back to Linux |
| right-click a device in Windows | same as click: take it back |
| `j` / `k`, `Enter` | keyboard: move, toggle |
| `x` | take the highlighted device back |
| `s` | start Windows, when it is off |

The row tells you where things stand: *In Windows*, *Sending…*, *Goes to
Windows when it starts*, *Not plugged in · still assigned*, or *Blocked ·
restart Windows once* (the VM was started with the old compose).

The icon in the bar fills in while any device is in Windows; hover for the count.

From a terminal:

```
winplug list              # what is plugged in and where it is
winplug add 1a86:7523     # or a list number, or part of the name
winplug remove ch341
winplug status
winplug doctor
winplug watch             # print every change as it happens
```

Devices that are part of the laptop (the Bluetooth radio, the camera) are
listed too, marked as such. Passing the Bluetooth radio through is one way to
get Bluetooth devices into Windows; that will be a proper feature later.

---

## What it does about hot-plugging

The earlier approach (a `-device usb-host` line in `ARGUMENTS`, plus
`devices: /dev/bus/usb` in the compose) had one failure that mattered: unplug
the device while Windows is running, plug it back in, and it never returned to
the VM until Windows was restarted.

Two things cause that, and Winplug fixes both.

**The container could not see the replugged device.** Docker's `devices:`
copies the device nodes that exist when the container starts. A replugged USB
device gets a new address, so a new node, and that node is not in the
container. Winplug bind-mounts `/dev/bus/usb` itself (the directory, so new
nodes appear) and adds a device-cgroup rule for USB character devices
(`c 189:* rmw`). Those are the only two lines it adds to Omarchy's compose file,
and it re-adds them if `omarchy windows vm install` rewrites the file.

**Nothing checked that QEMU actually re-attached.** QEMU matches `usb-host`
devices by vendor and product id and rescans every two seconds, so with the
nodes visible it usually re-attaches on its own. Winplug watches udev and, a few
seconds after a device is back on the host but still not in the guest, tears
the QEMU device down and recreates it. If that still fails it says so in the
row instead of leaving you to guess.

Devices are attached and detached live through QEMU's monitor
(`device_add` / `device_del` on the `xhci` controller dockur creates), reached
through the container's own mount namespace. No compose change, no container
restart, no `docker exec`.

## Latency

This is QEMU's native `usb-host` passthrough on the xHCI controller: the same
mechanism virt-manager uses when you add a "USB Host Device". The device is
detached from its Linux driver and handed to the guest whole, including bulk and
interrupt transfers with their real timing. It is what people flash ECUs
through. Two honest notes:

- The device is the guest's only while it is *In Windows*. Do not take it back
  mid-flash. Winplug will not do it for you either: assignments survive
  everything except your click.
- USB *mass storage* can be handled either way; for a plain file exchange the
  `~/Windows` shared folder is simpler. And dockur's warning stands: never have
  a USB drive attached while Windows Setup is still running.

## What it is not

- Not for hubs. Hubs are hidden; plug the device in and send the device.
- Two identical devices (same vendor and product id) show as one entry and
  QEMU takes the first it finds. Serial-number matching is on the list.
- Not Bluetooth yet. See above for the radio-passthrough workaround.

---

## Files

```
Panel.qml              the bar widget and popup (Quickshell, Omarchy's UI kit)
manifest.json          Omarchy plugin manifest
install.sh             user half: enable the widget
system/winplugd.py     root service: udev watch, QEMU monitor, compose patch
system/winplug         CLI
system/install.sh      root half: service, config, CLI
system/uninstall.sh    root half removal, restores the compose
tools/                 tests: unit tests and an end-to-end run against a fake QEMU monitor
```

`winplugd` keeps its assignments in `/var/lib/winplug/state.json` and listens
on `/run/winplug/winplug.sock`, owned by the user named in
`/etc/winplug/winplug.conf`. The protocol is newline-delimited JSON; `winplug
list --json` shows the whole state message.

### A side effect you will like

Omarchy's launcher requires `~/Windows` to be mode `0700` exactly, but dockur
sets an empty shared folder to `2777`, and `chmod 0700` leaves the setgid bit
in place. The result is that the *second* launch fails with no message.
`winplugd` clears the bit whenever it sees it, so `omarchy windows vm launch`
keeps working. (Reported upstream; set `fix_home_modes = false` in the config
to turn this off.)

## Tests

```bash
python3 tools/test_winplugd.py    # compose patching, sysfs parsing, HMP parsing
tools/test-daemon.sh              # the daemon against a fake QEMU monitor, as a normal user
```

## License

MIT.
