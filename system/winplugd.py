#!/usr/bin/env python3
"""winplugd -- USB passthrough helper for the Omarchy Windows VM.

Runs as root (systemd), owns three jobs:

  1. Keep a list of "assigned" USB devices (by vendor:product id) and make
     sure every assigned device that is plugged into the laptop is attached to
     the Windows VM's QEMU, whenever that VM is running.  Attach and detach go
     through QEMU's human monitor (HMP) inside the dockur container, reached
     via /proc/<container pid>/root, so nothing needs a VM restart and nothing
     needs the docker group.

  2. Watch udev.  A dongle that is unplugged and plugged back in gets a new
     device node; QEMU re-opens it by vendor:product on its own, and this
     daemon checks that it actually did, and recreates the device if not.
     This is the "replugged device never came back" bug the earlier version
     had.

  3. Make the container *able* to see hot-plugged devices at all: Omarchy's
     compose only lists /dev/kvm and /dev/net/tun.  Docker's `devices:`
     snapshot the nodes at container start, so a replugged device is
     invisible.  The daemon adds a bind mount of /dev/bus/usb plus a device
     cgroup rule for USB character devices (major 189) to the compose file.
     That takes effect the next time the VM is started; until then the panel
     says so.

Clients (the bar plugin, the `winplug` CLI) talk newline-delimited JSON over a
unix socket that is owned by the configured user.  Every client receives the
full state on connect and again whenever anything changes.

Nothing here is specific to the Framework 12; the only Omarchy-specific parts
are the container name and the compose path, both in the config file.
"""

import configparser
import errno
import json
import logging
import os
import pwd
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import time

log = logging.getLogger("winplugd")

CONF_PATH = os.environ.get("WINPLUG_CONF", "/etc/winplug/winplug.conf")
PROTOCOL_VERSION = 1
QEMU_ID_PREFIX = "winplug-"
USB_MAJOR = 189
TICK_SECONDS = 2.0
DOCKER_POLL_SECONDS = 3.0
# How long a plugged-in, assigned device may sit unattached before the
# usb-host device is torn down and recreated.  QEMU's own re-scan runs every
# two seconds, so anything past this is a device it will not pick up again.
REATTACH_AFTER_SECONDS = 7.0
# A device that is still not attached this long after the container clearly
# has the device nodes points at the cgroup rule, i.e. the VM was started with
# the old compose.
NO_ACCESS_AFTER_SECONDS = 12.0


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser()
        cp.read_dict({"winplug": {
            "user": "",
            "socket": "/run/winplug/winplug.sock",
            "state": "/var/lib/winplug/state.json",
            "container": "omarchy-windows",
            "compose": "/var/lib/omarchy/windows/docker-compose.yml",
            "docker": "/usr/bin/docker",
            "usb_bus": "xhci.0",
            "fix_home_modes": "true",
        }})
        if os.path.exists(path):
            cp.read(path)
        s = cp["winplug"]
        env = os.environ.get
        self.user = env("WINPLUG_USER", s["user"])
        self.socket = env("WINPLUG_SOCKET", s["socket"])
        self.state = env("WINPLUG_STATE", s["state"])
        self.container = env("WINPLUG_CONTAINER", s["container"])
        self.compose = env("WINPLUG_COMPOSE", s["compose"])
        self.docker = env("WINPLUG_DOCKER", s["docker"])
        self.usb_bus = env("WINPLUG_USB_BUS", s["usb_bus"])
        self.fix_home_modes = s.getboolean("fix_home_modes") and not env("WINPLUG_NO_HOME_FIX")
        # Test hooks.  None of these are read from the config file on purpose.
        self.sysfs = env("WINPLUG_SYSFS", "/sys/bus/usb/devices")
        self.monitor_override = env("WINPLUG_MONITOR")        # absolute HMP socket path
        self.container_pid_override = env("WINPLUG_CONTAINER_PID")
        self.no_udev = bool(env("WINPLUG_NO_UDEV"))
        self.uid = None
        self.gid = None
        self.home = None
        if self.user:
            try:
                pw = pwd.getpwnam(self.user)
                self.uid, self.gid, self.home = pw.pw_uid, pw.pw_gid, pw.pw_dir
            except KeyError:
                log.error("configured user %r does not exist", self.user)


# --------------------------------------------------------------------------
# Host USB enumeration (sysfs, no lsusb needed)
# --------------------------------------------------------------------------

def _read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def device_key(vid, pid):
    return "%s:%s" % (vid, pid)


def qemu_id(key):
    return QEMU_ID_PREFIX + key.replace(":", "-")


def enumerate_usb(sysfs):
    """Every USB device on the host that is not a hub, keyed by vid:pid.

    Two identical devices (same vendor and product) collapse into one entry
    with count > 1: QEMU matches usb-host devices by vendor:product too, so
    the panel would not be able to tell them apart either."""
    devices = {}
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return devices
    for name in names:
        # Devices look like "3-7" or "3-7.2"; interfaces like "3-7:1.0";
        # root hubs like "usb3".
        if not re.fullmatch(r"\d+-[\d.]+", name):
            continue
        d = os.path.join(sysfs, name)
        vid = _read(d + "/idVendor").lower()
        pid = _read(d + "/idProduct").lower()
        if not re.fullmatch(r"[0-9a-f]{4}", vid) or not re.fullmatch(r"[0-9a-f]{4}", pid):
            continue
        dev_class = _read(d + "/bDeviceClass")
        if dev_class == "09":       # hub
            continue
        drivers = set()
        for entry in os.listdir(d) if os.path.isdir(d) else []:
            if re.fullmatch(re.escape(name) + r":\d+\.\d+", entry):
                drv = os.path.join(d, entry, "driver")
                if os.path.islink(drv):
                    drivers.add(os.path.basename(os.readlink(drv)))
        manufacturer = _read(d + "/manufacturer")
        product = _read(d + "/product")
        serial = _read(d + "/serial")
        try:
            busnum = int(_read(d + "/busnum") or 0)
            devnum = int(_read(d + "/devnum") or 0)
        except ValueError:
            busnum = devnum = 0
        key = device_key(vid, pid)
        entry = {
            "key": key,
            "vid": vid,
            "pid": pid,
            "manufacturer": manufacturer,
            "product": product,
            "serial": serial,
            "speed": _read(d + "/speed"),
            "bus": busnum,
            "dev": devnum,
            "port": name.split("-", 1)[1],
            "sysname": name,
            "class": dev_class,
            "drivers": sorted(drivers),
            "count": 1,
        }
        entry["name"] = display_name(entry)
        entry["tags"] = tags_for(entry)
        if key in devices:
            devices[key]["count"] += 1
        else:
            devices[key] = entry
    return devices


def display_name(d):
    prod = d["product"].strip()
    man = d["manufacturer"].strip()
    if prod and man and not prod.lower().startswith(man.lower()):
        return "%s %s" % (man, prod)
    return prod or man or "USB device %s" % d["key"]


# Devices that are part of the laptop rather than something plugged into it.
# They are still listed -- passing the Bluetooth radio through is how a later
# version does Bluetooth -- but the panel marks them so they are not sent over
# by accident.
def tags_for(d):
    tags = []
    drivers = set(d["drivers"])
    if "btusb" in drivers:
        tags.append("bluetooth radio")
    if "uvcvideo" in drivers:
        tags.append("camera")
    if "usbhid" in drivers:
        tags.append("input")
    if "usb-storage" in drivers or "uas" in drivers:
        tags.append("storage")
    if drivers & {"cdc_acm", "ftdi_sio", "ch341", "cp210x", "pl2303", "usbserial"}:
        tags.append("serial")
    if d["speed"] in ("5000", "10000", "20000"):
        tags.append("USB 3")
    return tags


# --------------------------------------------------------------------------
# QEMU human monitor client
# --------------------------------------------------------------------------

class HmpError(Exception):
    pass


PROMPT = b"(qemu) "


def hmp(path, command, timeout=5.0):
    """Run one HMP command over the monitor socket and return its output.

    One short-lived connection per command.  The container's own shutdown
    handler uses the same socket, and QEMU serves one client at a time, so a
    connection that is held open would block the VM from powering down."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        _read_until(s, PROMPT)               # banner + first prompt
        s.sendall(command.encode() + b"\n")
        out = _read_until(s, PROMPT)
    except (OSError, socket.timeout) as e:
        raise HmpError(str(e))
    finally:
        try:
            s.close()
        except OSError:
            pass
    text = out[: -len(PROMPT)].decode(errors="replace").replace("\r", "")
    lines = text.split("\n")
    # readline echoes the command back on the first line.
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    return "\n".join(l for l in lines if l.strip() != "").strip()


def _read_until(s, marker):
    buf = b""
    while not buf.endswith(marker):
        chunk = s.recv(4096)
        if not chunk:
            raise HmpError("monitor closed the connection")
        buf += chunk
    return buf


def parse_info_usb(text):
    """IDs of the devices currently attached to the guest."""
    return set(re.findall(r"ID:\s*(\S+)", text))


# --------------------------------------------------------------------------
# Container / VM tracking
# --------------------------------------------------------------------------

MONITOR_CANDIDATES = ("dev/shm/monitor.sock", "run/shm/monitor.sock")


def container_path(pid, rel):
    """Resolve `rel` inside the container's root, following symlinks *within*
    that root.  A plain /proc/<pid>/root/<rel> would send an absolute symlink
    (dockur links /run/shm -> /dev/shm) back out to the host."""
    root = "/proc/%d/root" % pid
    parts = [p for p in rel.split("/") if p]
    cur = root
    hops = 0
    while parts:
        part = parts.pop(0)
        cand = cur + "/" + part
        try:
            target = os.readlink(cand)
        except OSError:
            cur = cand
            continue
        hops += 1
        if hops > 16:
            return None
        if target.startswith("/"):
            cur = root
            parts = [p for p in target.split("/") if p] + parts
        else:
            parts = [p for p in target.split("/") if p] + parts
    return cur


class Vm:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pid = 0
        self.status = "unknown"        # docker's State.Status, or "absent"
        self.installed = os.path.exists(cfg.compose)
        self.monitor = None
        self.usb_dir = None            # container's /dev/bus/usb, if bound
        self._last_docker = 0.0
        self.error = ""

    @property
    def running(self):
        return self.status == "running" and self.pid > 0

    def refresh(self, now):
        self.installed = os.path.exists(self.cfg.compose) or bool(self.cfg.monitor_override)
        if self.cfg.container_pid_override:
            self.pid = int(self.cfg.container_pid_override)
            self.status = "running" if os.path.exists("/proc/%d" % self.pid) else "exited"
        else:
            if self.pid > 0 and os.path.exists("/proc/%d" % self.pid) and now - self._last_docker < 30:
                pass  # still alive, do not bother docker
            elif now - self._last_docker >= DOCKER_POLL_SECONDS:
                self._last_docker = now
                self._ask_docker()
        if self.running:
            self._locate()
        else:
            self.monitor = None
            self.usb_dir = None

    def _ask_docker(self):
        if not os.path.exists(self.cfg.docker):
            self.status, self.pid, self.error = "absent", 0, "docker not installed"
            return
        try:
            out = subprocess.run(
                [self.cfg.docker, "inspect", "-f", "{{.State.Pid}} {{.State.Status}}", self.cfg.container],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.status, self.pid, self.error = "unknown", 0, "docker: %s" % e
            return
        if out.returncode != 0:
            err = out.stderr.strip()
            if "No such object" in err or "No such container" in err:
                self.status, self.pid, self.error = "absent", 0, ""
            elif "Cannot connect" in err or "permission denied" in err:
                self.status, self.pid, self.error = "unknown", 0, "docker daemon not running"
            else:
                self.status, self.pid, self.error = "unknown", 0, err[:200]
            return
        self.error = ""
        try:
            pid_s, status = out.stdout.split()
            self.pid = int(pid_s)
            self.status = status
        except ValueError:
            self.pid, self.status = 0, "unknown"
        if self.pid <= 0:
            self.status = "exited" if self.status == "running" else self.status

    def _locate(self):
        if self.cfg.monitor_override:
            self.monitor = self.cfg.monitor_override if os.path.exists(self.cfg.monitor_override) else None
        else:
            self.monitor = None
            for rel in MONITOR_CANDIDATES:
                p = container_path(self.pid, rel)
                if p and os.path.exists(p):
                    self.monitor = p
                    break
        usb = container_path(self.pid, "dev/bus/usb")
        self.usb_dir = usb if usb and os.path.isdir(usb) else None

    def sees_device(self, d):
        """Whether the container has this device's node.  With the bind mount
        in place it always does; without it the node is missing."""
        if self.cfg.sysfs != "/sys/bus/usb/devices" or (self.cfg.monitor_override and not self.usb_dir):
            return True   # test mode: fake devices have no nodes to check
        if not self.usb_dir:
            return False
        return os.path.exists("%s/%03d/%03d" % (self.usb_dir, d["bus"], d["dev"]))


# --------------------------------------------------------------------------
# Compose file patch
# --------------------------------------------------------------------------

USB_VOLUME = "/dev/bus/usb:/dev/bus/usb"
CGROUP_RULE = 'c %d:* rmw' % USB_MAJOR


def compose_has_usb(text):
    return USB_VOLUME in text and "device_cgroup_rules" in text and ("%d:*" % USB_MAJOR) in text


def patch_compose_text(text):
    """Add the USB bind mount and cgroup rule to Omarchy's compose.  Returns
    the new text, or None if the file does not look like the expected compose
    (in which case leave it alone rather than guess)."""
    if compose_has_usb(text):
        return text
    lines = text.split("\n")
    # Find the service block indentation from the `volumes:` line.
    vol_idx = None
    for i, l in enumerate(lines):
        if re.fullmatch(r"\s+volumes:\s*", l):
            vol_idx = i
            break
    if vol_idx is None:
        return None
    indent = re.match(r"\s*", lines[vol_idx]).group(0)
    item_indent = indent + "  "
    # End of the volumes list.
    j = vol_idx + 1
    while j < len(lines) and (lines[j].startswith(item_indent + "-") or lines[j].strip() == ""):
        if lines[j].strip() == "":
            break
        j += 1
    if USB_VOLUME not in text:
        lines.insert(j, item_indent + "- " + USB_VOLUME)
        j += 1
    if "device_cgroup_rules" not in text:
        lines.insert(j, indent + "device_cgroup_rules:")
        lines.insert(j + 1, item_indent + '- "' + CGROUP_RULE + '"')
    return "\n".join(lines)


def ensure_compose(path):
    """Patch the compose in place, keeping owner and mode.  Returns
    (changed, error)."""
    try:
        st = os.stat(path)
        with open(path) as f:
            text = f.read()
    except OSError as e:
        return False, str(e)
    if compose_has_usb(text):
        return False, ""
    new = patch_compose_text(text)
    if new is None:
        return False, "compose file has no volumes: block; not touching it"
    tmp = path + ".winplug.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, str(e)
    return True, ""


# --------------------------------------------------------------------------
# Persistent assignments
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self.path = path
        self.assigned = {}     # key -> {"name": str, "added": float}
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.assigned = {k: v for k, v in data.get("assigned", {}).items()
                             if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", k)}
        except (OSError, ValueError):
            self.assigned = {}

    def save(self):
        d = os.path.dirname(self.path)
        try:
            os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 1, "assigned": self.assigned}, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("cannot save state to %s: %s", self.path, e)


# --------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------

class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.vm = Vm(cfg)
        self.store = Store(cfg.state)
        self.clients = {}            # sock -> {"buf": bytes}
        self.udev = None             # udevadm subprocess
        self.udev_buf = b""
        self.pending_reconcile_at = 0.0
        self.last_state_json = ""
        self.status = {}             # key -> status string
        self.detail = {}             # key -> human detail
        self.unattached_since = {}   # key -> time first seen present-but-unattached
        self.recreate = set()        # keys whose qdev was deleted and must be re-added
        self.compose_patched = False
        self.compose_error = ""
        self.compose_checked = 0.0
        self.home_checked = 0.0
        self.running = True
        self.listener = None
        self.socket_id = None
        self.last_hmp_error = ""

    # -- lifecycle ---------------------------------------------------------

    def run(self):
        self.open_socket()
        if not self.cfg.no_udev:
            self.start_udev()
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        log.info("winplugd ready on %s (container %s, user %s)",
                 self.cfg.socket, self.cfg.container, self.cfg.user or "-")
        next_tick = 0.0
        while self.running:
            now = time.monotonic()
            timeout = max(0.05, min(next_tick - now,
                                    (self.pending_reconcile_at - now) if self.pending_reconcile_at else 60))
            rlist = [self.listener] + list(self.clients)
            if self.udev and self.udev.stdout:
                rlist.append(self.udev.stdout)
            try:
                ready, _, _ = select.select(rlist, [], [], timeout)
            except InterruptedError:
                ready = []
            except OSError as e:
                if e.errno == errno.EINTR:
                    ready = []
                else:
                    raise
            for r in ready:
                if r is self.listener:
                    self.accept()
                elif self.udev and r is self.udev.stdout:
                    self.read_udev()
                else:
                    self.read_client(r)
            now = time.monotonic()
            if now >= next_tick or (self.pending_reconcile_at and now >= self.pending_reconcile_at):
                self.pending_reconcile_at = 0.0
                next_tick = now + TICK_SECONDS
                self.tick()
        self.close()

    def stop(self):
        self.running = False

    def close(self):
        for c in list(self.clients):
            c.close()
        if self.listener:
            self.listener.close()
            # Only remove the socket file if it is still the one we created; a
            # replacement daemon may already have bound a new one at the path.
            try:
                st = os.lstat(self.cfg.socket)
                if (st.st_dev, st.st_ino) == self.socket_id:
                    os.unlink(self.cfg.socket)
            except OSError:
                pass
        if self.udev:
            self.udev.terminate()

    def open_socket(self):
        path = self.cfg.socket
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)
        except OSError:
            pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        st = os.lstat(path)
        self.socket_id = (st.st_dev, st.st_ino)
        self.listener.listen(8)
        self.listener.setblocking(False)
        if self.cfg.uid is not None and os.geteuid() == 0:
            os.chown(path, self.cfg.uid, self.cfg.gid)
        os.chmod(path, 0o660)

    def start_udev(self):
        try:
            self.udev = subprocess.Popen(
                ["udevadm", "monitor", "--udev", "--subsystem-match=usb", "--property"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.set_blocking(self.udev.stdout.fileno(), False)
        except OSError as e:
            log.warning("udevadm monitor unavailable (%s); falling back to polling", e)
            self.udev = None

    def read_udev(self):
        try:
            chunk = os.read(self.udev.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        if not chunk:
            log.warning("udevadm monitor exited; polling only")
            self.udev = None
            return
        self.udev_buf += chunk
        while b"\n\n" in self.udev_buf:
            block, self.udev_buf = self.udev_buf.split(b"\n\n", 1)
            props = dict(l.split(b"=", 1) for l in block.split(b"\n") if b"=" in l)
            if props.get(b"DEVTYPE") == b"usb_device" and props.get(b"ACTION") in (b"add", b"remove", b"bind"):
                # Give the kernel a moment to finish binding drivers, then look.
                self.schedule_reconcile(0.4)

    def schedule_reconcile(self, delay):
        at = time.monotonic() + delay
        if not self.pending_reconcile_at or at < self.pending_reconcile_at:
            self.pending_reconcile_at = at

    # -- clients -----------------------------------------------------------

    def accept(self):
        try:
            c, _ = self.listener.accept()
        except OSError:
            return
        c.setblocking(False)
        self.clients[c] = {"buf": b""}
        self.send(c, self.state_message())

    def read_client(self, c):
        try:
            data = c.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self.drop(c)
            return
        st = self.clients[c]
        st["buf"] += data
        if len(st["buf"]) > 1 << 20:
            self.drop(c)
            return
        while b"\n" in st["buf"]:
            line, st["buf"] = st["buf"].split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line.decode())
                if not isinstance(req, dict):
                    raise ValueError("not an object")
            except ValueError as e:
                self.send(c, {"type": "reply", "ok": False, "error": "bad request: %s" % e})
                continue
            self.handle(c, req)

    def drop(self, c):
        self.clients.pop(c, None)
        try:
            c.close()
        except OSError:
            pass

    def send(self, c, msg):
        try:
            c.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except OSError:
            self.drop(c)

    def broadcast_state(self, force=False):
        msg = self.state_message()
        j = json.dumps(msg, sort_keys=True)
        if not force and j == self.last_state_json:
            return
        self.last_state_json = j
        for c in list(self.clients):
            self.send(c, msg)

    def handle(self, c, req):
        cmd = req.get("cmd")
        rid = req.get("id")
        def reply(ok, error="", **extra):
            m = {"type": "reply", "cmd": cmd, "ok": ok}
            if rid is not None:
                m["id"] = rid
            if error:
                m["error"] = error
            m.update(extra)
            self.send(c, m)
        if cmd == "state":
            reply(True)
            self.send(c, self.state_message())
        elif cmd == "attach":
            key = self.normalize_key(req.get("key"))
            if not key:
                return reply(False, "key must look like 1a2b:3c4d")
            present = enumerate_usb(self.cfg.sysfs)
            name = req.get("name") or (present[key]["name"] if key in present else key)
            self.store.assigned[key] = {"name": str(name)[:120], "added": time.time()}
            self.store.save()
            self.unattached_since.pop(key, None)
            log.info("assigned %s (%s)", key, name)
            reply(True)
            self.tick()
        elif cmd == "detach":
            key = self.normalize_key(req.get("key"))
            if not key:
                return reply(False, "key must look like 1a2b:3c4d")
            was = self.store.assigned.pop(key, None)
            self.store.save()
            self.status[key] = "detaching"
            err = self.qemu_remove(key)
            log.info("unassigned %s (%s)%s", key, was["name"] if was else "?", (" -- " + err) if err else "")
            reply(not err, err)
            self.tick()
        elif cmd == "ensure_compose":
            changed, err = self.check_compose(force=True)
            reply(not err, err, changed=changed)
            self.broadcast_state(force=True)
        elif cmd == "ping":
            reply(True)
        else:
            reply(False, "unknown command %r" % cmd)

    @staticmethod
    def normalize_key(key):
        if not isinstance(key, str):
            return None
        key = key.strip().lower()
        m = re.fullmatch(r"(?:0x)?([0-9a-f]{4}):(?:0x)?([0-9a-f]{4})", key)
        return device_key(m.group(1), m.group(2)) if m else None

    # -- periodic work -----------------------------------------------------

    def tick(self):
        now = time.monotonic()
        self.vm.refresh(now)
        if now - self.compose_checked > 10:
            self.compose_checked = now
            self.check_compose()
        if self.cfg.fix_home_modes and now - self.home_checked > 5:
            self.home_checked = now
            self.fix_home_modes()
        try:
            self.reconcile()
        except Exception:      # never let one bad tick kill the daemon
            log.exception("reconcile failed")
        self.broadcast_state()

    def check_compose(self, force=False):
        if not os.path.exists(self.cfg.compose):
            self.compose_patched = False
            self.compose_error = ""
            return False, "compose not found (Windows VM not installed)"
        changed, err = ensure_compose(self.cfg.compose)
        self.compose_error = err
        if changed:
            log.info("added USB bind mount and cgroup rule to %s", self.cfg.compose)
        try:
            with open(self.cfg.compose) as f:
                self.compose_patched = compose_has_usb(f.read())
        except OSError:
            self.compose_patched = False
        return changed, err

    def fix_home_modes(self):
        """Omarchy's launcher requires ~/Windows and ~/.windows to be mode
        exactly 0700, but dockur sets the shared folder to 2777 whenever it is
        empty, and GNU chmod 0700 keeps that setgid bit.  Result: the second
        launch fails with no message.  Clear the bits so launch keeps working."""
        if not self.cfg.home or self.cfg.uid is None or os.geteuid() != 0:
            return
        for sub in ("Windows", ".windows"):
            p = os.path.join(self.cfg.home, sub)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != self.cfg.uid:
                continue
            if st.st_mode & (stat.S_ISGID | stat.S_ISUID):
                try:
                    os.chmod(p, 0o700)
                    log.info("cleared setgid on %s so 'omarchy windows vm launch' works again", p)
                except OSError as e:
                    log.warning("could not chmod %s: %s", p, e)

    # -- the core: make QEMU match the assignment list -----------------------

    def reconcile(self):
        present = enumerate_usb(self.cfg.sysfs)
        self.present = present
        assigned = self.store.assigned
        vm = self.vm
        now = time.monotonic()

        if not (vm.running and vm.monitor):
            for key in assigned:
                self.status[key] = "waiting-vm" if key in present else "unplugged"
            self.unattached_since.clear()
            self.recreate.clear()
            return

        try:
            attached = parse_info_usb(hmp(vm.monitor, "info usb"))
            self.last_hmp_error = ""
        except HmpError as e:
            self.last_hmp_error = str(e)
            for key in assigned:
                self.status[key] = "waiting-vm"
            return

        for key, info in assigned.items():
            qid = qemu_id(key)
            dev = present.get(key)
            if dev is None:
                # Leave the usb-host device in place: QEMU re-opens the device
                # by vendor:product the moment it comes back.
                self.status[key] = "unplugged"
                self.unattached_since.pop(key, None)
                continue
            if qid in attached:
                self.status[key] = "attached"
                self.detail[key] = ""
                self.unattached_since.pop(key, None)
                self.recreate.discard(key)
                continue
            # Present on the host but not in the guest.
            if not vm.sees_device(dev):
                self.status[key] = "no-access"
                self.detail[key] = "the VM was started without USB access; restart Windows"
                continue
            since = self.unattached_since.setdefault(key, now)
            if key in self.recreate:
                # A deleted qdev may take a moment to go; keep trying to add.
                err = self.qemu_add(key, dev)
                if err and "Duplicate" in err:
                    self.status[key] = "attaching"
                    continue
                # Keep the original timestamp: the clock towards "no-access"
                # runs from when the device first sat unattached, not from
                # the recreate.
                self.recreate.discard(key)
                self.status[key] = "attaching" if not err else "error"
                self.detail[key] = err
                continue
            err = self.qemu_add(key, dev)
            if not err:
                self.status[key] = "attaching"
                self.detail[key] = ""
                continue
            if "Duplicate" in err:
                # The device exists in QEMU but is not attached: the host
                # device came back (new node) and QEMU has not picked it up,
                # or cannot open it.
                waited = now - since
                if waited > NO_ACCESS_AFTER_SECONDS:
                    self.status[key] = "no-access"
                    self.detail[key] = "QEMU cannot open the device; restart Windows so the USB rule applies"
                elif waited > REATTACH_AFTER_SECONDS:
                    log.info("%s is plugged in but not attached after %.0fs; recreating", key, waited)
                    self.qemu_remove(key)
                    self.recreate.add(key)
                    self.status[key] = "attaching"
                else:
                    self.status[key] = "attaching"
                continue
            self.status[key] = "error"
            self.detail[key] = err
            log.warning("device_add %s failed: %s", key, err)

        # Anything of ours in the guest that is no longer assigned goes away
        # (an unassign that happened while the VM was unreachable).
        for qid in attached:
            if qid.startswith(QEMU_ID_PREFIX):
                key = qid[len(QEMU_ID_PREFIX):].replace("-", ":", 1)
                if key not in assigned:
                    log.info("removing stale %s from the guest", qid)
                    self.qemu_remove(key)

    def qemu_add(self, key, dev):
        """device_add; returns an error string, empty on success."""
        base = "device_add usb-host,vendorid=0x%s,productid=0x%s,id=%s" % (dev["vid"], dev["pid"], qemu_id(key))
        cmds = [base + ",bus=" + self.cfg.usb_bus, base] if self.cfg.usb_bus else [base]
        err = ""
        for cmd in cmds:
            try:
                out = hmp(self.vm.monitor, cmd)
            except HmpError as e:
                return "monitor: %s" % e
            if not out:
                return ""
            err = out
            if "Duplicate" in out:
                return out
            if "Bus" in out and "not found" in out:
                continue        # retry without an explicit bus
            return out
        return err

    def qemu_remove(self, key):
        if not (self.vm.running and self.vm.monitor):
            return ""
        try:
            out = hmp(self.vm.monitor, "device_del " + qemu_id(key))
        except HmpError as e:
            return "monitor: %s" % e
        if out and "not found" in out.lower():
            return ""
        return out

    # -- state snapshot ----------------------------------------------------

    def state_message(self):
        vm = self.vm
        present = getattr(self, "present", None)
        if present is None:
            present = enumerate_usb(self.cfg.sysfs)
        devices = []
        keys = list(present.keys()) + [k for k in self.store.assigned if k not in present]
        for key in keys:
            d = present.get(key)
            assigned = key in self.store.assigned
            entry = {
                "key": key,
                "vid": key[:4],
                "pid": key[5:],
                "present": d is not None,
                "assigned": assigned,
                "status": self.status.get(key, "unplugged") if assigned else "available",
                "detail": self.detail.get(key, "") if assigned else "",
            }
            if d:
                entry.update({k: d[k] for k in ("name", "manufacturer", "product", "serial", "speed", "bus", "port", "tags", "count", "drivers")})
            else:
                entry.update({"name": self.store.assigned[key].get("name", key), "manufacturer": "", "product": "",
                              "serial": "", "speed": "", "bus": 0, "port": "", "tags": [], "count": 0, "drivers": []})
            if not assigned:
                entry["status"] = "available"
            devices.append(entry)
        devices.sort(key=lambda e: (not e["assigned"], not e["present"], e["name"].lower()))

        if not vm.installed:
            vstatus, message = "not-installed", "Windows VM is not installed (omarchy windows vm install)"
        elif vm.status == "running":
            if vm.monitor:
                if self.last_hmp_error:
                    vstatus, message = "starting", "Windows is starting"
                elif vm.usb_dir is None:
                    vstatus, message = "running-no-usb", "Windows is running without USB access; restart it once"
                else:
                    vstatus, message = "running", "Windows is running"
            else:
                vstatus, message = "starting", "Windows is starting"
        elif vm.status in ("exited", "created", "absent", "dead", "paused", "removing", "restarting"):
            vstatus, message = "off", "Windows is not running"
        else:
            vstatus, message = "unknown", vm.error or "Cannot reach Docker"
        if self.compose_error and vm.installed:
            message += " · compose: " + self.compose_error
        return {
            "type": "state",
            "version": PROTOCOL_VERSION,
            "vm": {
                "installed": vm.installed,
                "status": vstatus,
                "container_status": vm.status,
                "usb_access": (vm.usb_dir is not None) if vm.running else None,
                "compose_patched": self.compose_patched,
                "message": message,
            },
            "devices": devices,
            "assigned_count": len(self.store.assigned),
            "attached_count": sum(1 for k in self.store.assigned if self.status.get(k) == "attached"),
        }


def main():
    logging.basicConfig(level=logging.DEBUG if os.environ.get("WINPLUG_DEBUG") else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = Config(CONF_PATH)
    if os.geteuid() != 0 and not (cfg.monitor_override or cfg.container_pid_override or cfg.no_udev):
        log.warning("not running as root; this only works for tests with WINPLUG_* overrides")
    Daemon(cfg).run()


if __name__ == "__main__":
    main()
