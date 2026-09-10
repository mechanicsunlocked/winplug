#!/usr/bin/env python3
"""Unit tests for the pure parts of winplugd (no root, no VM)."""
import os, sys, json, tempfile, unittest, importlib.util, importlib.machinery, stat, socket, threading, subprocess, time, shutil

# Never write __pycache__ into the plugin directory: Omarchy's shell reloads
# the plugin on any file change there, and a reload while the screen is
# locked strands the lock screen (the user is locked out).
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("winplugd", os.path.join(here, "..", "system", "winplugd.py"))
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
# The CLI has no .py suffix; load it by hand.
_loader = importlib.machinery.SourceFileLoader("winplug_cli", os.path.join(here, "..", "system", "winplug"))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("winplug_cli", _loader)); _loader.exec_module(cli)
w.log.setLevel(60)   # the tests provoke expected error logs (e.g. a config user that does not exist)

OMARCHY_COMPOSE = """services:
  windows:
    image: dockurr/windows
    container_name: omarchy-windows
    environment:
      VERSION: "11"
      PROTECT: "Y"
      ARGUMENTS: "-rtc base=localtime,clock=host,driftfix=slew"
    devices:
      - /dev/kvm
      - /dev/net/tun
    cap_add:
      - NET_ADMIN
    ports:
      - 127.0.0.1:8006:8006
      - 127.0.0.1:3389:3389/tcp
      - 127.0.0.1:3389:3389/udp
    volumes:
      - /var/lib/omarchy/windows/mounts/users/1000/storage:/storage
      - /var/lib/omarchy/windows/mounts/users/1000/shared:/shared
    restart: "no"
    stop_grace_period: 2m
"""

class Compose(unittest.TestCase):
    def test_patch_adds_both(self):
        out = w.patch_compose_text(OMARCHY_COMPOSE)
        self.assertIn("      - /dev/bus/usb:/dev/bus/usb\n", out)
        self.assertIn('    device_cgroup_rules:\n      - "c 189:* rmw"\n', out)
        # Omarchy's own checks: exactly one /storage and one /shared mount, PROTECT once.
        self.assertEqual(out.count(":/storage\n"), 1)
        self.assertEqual(out.count(":/shared\n"), 1)
        self.assertEqual(out.count("PROTECT:"), 1)
        # Inserted inside the volumes list, before restart:
        v = out.index("volumes:"); u = out.index("/dev/bus/usb:"); r = out.index("restart:")
        self.assertTrue(v < u < r)
        self.assertTrue(w.compose_has_usb(out))

    def test_patch_is_idempotent(self):
        once = w.patch_compose_text(OMARCHY_COMPOSE)
        self.assertEqual(w.patch_compose_text(once), once)

    def test_patch_refuses_unknown_shape(self):
        self.assertIsNone(w.patch_compose_text("services:\n  x:\n    image: y\n"))

    def test_ensure_compose_keeps_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "docker-compose.yml")
            with open(p, "w") as f:
                f.write(OMARCHY_COMPOSE)
            os.chmod(p, 0o640)
            changed, err = w.ensure_compose(p)
            self.assertTrue(changed); self.assertEqual(err, "")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)
            changed, err = w.ensure_compose(p)
            self.assertFalse(changed); self.assertEqual(err, "")
            self.assertFalse(os.path.exists(p + ".winplug.tmp"))

    def test_uninstall_awk_reverts(self):
        import subprocess
        patched = w.patch_compose_text(OMARCHY_COMPOSE)
        out = subprocess.run(["awk", "-f", os.path.join(here, "..", "system", "compose-unpatch.awk")], input=patched, capture_output=True, text=True).stdout
        self.assertEqual(out, OMARCHY_COMPOSE)

def readline_echo(cmd):
    """QEMU's readline on the wire: a redraw of the line after every byte."""
    out = b""; shown = 0
    for i in range(1, len(cmd) + 1):
        out += b"\x1b[D" * shown + cmd[:i].encode() + b"\x1b[K"; shown = i
    return out

class Hmp(unittest.TestCase):
    def test_output_strips_readline_echo(self):
        cmd = "device_add usb-host,vendorid=0x1a86,productid=0x7523,id=winplug-1a86-7523,bus=xhci.0"
        # Success: nothing but the echo comes back.
        self.assertEqual(w.hmp_output(readline_echo(cmd) + b"\r\n(qemu) ", cmd), "")
        # An error after the echo survives intact.
        raw = readline_echo(cmd) + b"\r\nDuplicate device ID 'winplug-1a86-7523'\r\n(qemu) "
        self.assertEqual(w.hmp_output(raw, cmd), "Duplicate device ID 'winplug-1a86-7523'")
        # A monitor that echoes plainly, or not at all.
        self.assertEqual(w.hmp_output(cmd.encode() + b"\r\nBus 'xhci.0' not found\r\n(qemu) ", cmd), "Bus 'xhci.0' not found")
        self.assertEqual(w.hmp_output(b"Bus 'xhci.0' not found\r\n(qemu) ", cmd), "Bus 'xhci.0' not found")
        # Multi-line output keeps every line.
        usb = ("  Device 0.1, Port 1, Speed 480 Mb/s, Product QEMU USB Tablet, ID: usb-tablet\r\n"
               "  Device 0.2, Port 2, Speed 12 Mb/s, Product USB2.0-Serial, ID: winplug-1a86-7523\r\n")
        out = w.hmp_output(readline_echo("info usb") + b"\r\n" + usb.encode() + b"(qemu) ", "info usb")
        self.assertEqual(w.parse_info_usb(out), {"usb-tablet", "winplug-1a86-7523"})
        self.assertEqual(out.count("\n"), 1)

    def test_output_on_a_real_qemu_echo(self):
        # Captured from the QEMU behind dockur/windows on 2026-09-05: the
        # reply to a device_add that succeeded -- 14 KB of redraw, no output.
        with open(os.path.join(here, "fixtures", "hmp-echo-device_add.bin"), "rb") as f:
            raw = f.read()
        cmd = "device_add usb-host,vendorid=0x32ac,productid=0x001d,id=winplug-32ac-001d,bus=xhci.0"
        self.assertTrue(raw.endswith(cmd.encode() + b"\x1b[K"))
        self.assertEqual(w.hmp_output(raw + b"\r\n(qemu) ", cmd), "")
        self.assertEqual(w.hmp_output(raw + b"\r\nDevice 'winplug-32ac-001d' not found\r\n(qemu) ", cmd),
                         "Device 'winplug-32ac-001d' not found")

    def test_hmp_end_to_end_against_a_readline_monitor(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "monitor.sock")
        srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(2)
        def serve(reply):
            c, _ = srv.accept()
            c.sendall(b"QEMU 10.0.0 monitor - type 'help' for more information\r\n(qemu) ")
            buf = b""
            while b"\n" not in buf:
                buf += c.recv(4096)
            cmd = buf.split(b"\n", 1)[0].decode()
            c.sendall(readline_echo(cmd) + b"\r\n" + reply + b"(qemu) ")
            c.close()
        cmd = "device_del winplug-1a86-7523"
        t = threading.Thread(target=serve, args=(b"",)); t.start()
        self.assertEqual(w.hmp(path, cmd, timeout=3), ""); t.join()
        t = threading.Thread(target=serve, args=(b"Device 'winplug-1a86-7523' not found\r\n",)); t.start()
        self.assertEqual(w.hmp(path, cmd, timeout=3), "Device 'winplug-1a86-7523' not found"); t.join()
        srv.close()
        with self.assertRaises(w.HmpError):
            w.hmp(path, "info usb", timeout=1)                 # nobody listening
        with self.assertRaises(w.HmpError):
            w.hmp(path, "info usb\nquit", timeout=1)           # never send a second line

    def test_parse_info_usb(self):
        txt = ("  Device 0.1, Port 1, Speed 480 Mb/s, Product QEMU USB Tablet, ID: usb-tablet\n"
               "  Device 0.2, Port 2, Speed 12 Mb/s, Product USB2.0-Serial, ID: winplug-1a86-7523\n")
        self.assertEqual(w.parse_info_usb(txt), {"usb-tablet", "winplug-1a86-7523"})
        self.assertEqual(w.parse_info_usb(""), set())

    def test_classify_docker_failure(self):
        # Docker 29 lower-cases this; older Docker capitalised it. Both = off.
        self.assertEqual(w.classify_docker_failure("Error: no such object: omarchy-windows"), ("absent", ""))
        self.assertEqual(w.classify_docker_failure("Error: No such object: omarchy-windows"), ("absent", ""))
        self.assertEqual(w.classify_docker_failure("Error: No such container: omarchy-windows"), ("absent", ""))
        self.assertEqual(w.classify_docker_failure("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"),
                         ("unknown", "docker daemon not running"))
        self.assertEqual(w.classify_docker_failure("permission denied while trying to connect")[0], "unknown")
        self.assertEqual(w.classify_docker_failure("weird failure")[1], "weird failure")

    def test_ids(self):
        self.assertEqual(w.qemu_id("1a86:7523"), "winplug-1a86-7523")
        self.assertEqual(w.Daemon.normalize_key("0x1A86:0X7523"), "1a86:7523")
        self.assertIsNone(w.Daemon.normalize_key("nope"))
        self.assertIsNone(w.Daemon.normalize_key(None))

class Sysfs(unittest.TestCase):
    def make(self, root, name, **attrs):
        d = os.path.join(root, name); os.makedirs(d)
        for k, v in attrs.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")
        return d

    def test_enumerate_skips_hubs_and_interfaces(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "usb3", idVendor="1d6b", idProduct="0003", bDeviceClass="09")
            self.make(root, "3-1", idVendor="1d6b", idProduct="0002", bDeviceClass="09")
            d = self.make(root, "3-7", idVendor="1a86", idProduct="7523", bDeviceClass="ff",
                          manufacturer="QinHeng", product="USB2.0-Serial", busnum="3", devnum="9", speed="12")
            os.makedirs(os.path.join(d, "3-7:1.0")); os.symlink("../../../../bus/usb/drivers/ch341", os.path.join(d, "3-7:1.0", "driver"))
            self.make(root, "3-7:1.0", idVendor="zzzz", idProduct="0000")   # interface dir at top level
            self.make(root, "3-8", idVendor="1a86", idProduct="7523", bDeviceClass="ff", product="USB2.0-Serial", busnum="3", devnum="10")
            devs = w.enumerate_usb(root)
            self.assertEqual(set(devs), {"1a86:7523"})
            e = devs["1a86:7523"]
            self.assertEqual(e["name"], "QinHeng USB2.0-Serial")
            self.assertEqual(e["drivers"], ["ch341"])
            self.assertIn("serial", e["tags"])
            self.assertEqual(e["count"], 2)
            self.assertEqual((e["bus"], e["dev"], e["port"]), (3, 9, "7"))

    def test_enumerate_skips_what_it_cannot_read(self):
        # A device that vanishes (or cannot be read) mid-scan must not take
        # the whole scan, and with it the daemon, down.
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "3-7", idVendor="1a86", idProduct="7523", bDeviceClass="ff", product="ok", busnum="3", devnum="9")
            # Attributes readable, directory not listable: the attribute reads
            # succeed and the interface scan raises, which is the OSError path.
            bad = self.make(root, "3-8", idVendor="0403", idProduct="6001", bDeviceClass="00", product="gone", busnum="3", devnum="10")
            os.chmod(bad, 0o111)
            try:
                self.assertRaises(OSError, w._usb_entry, root, "3-8")
                devs = w.enumerate_usb(root)
            finally:
                os.chmod(bad, 0o755)
            self.assertEqual(set(devs), {"1a86:7523"})
            self.assertIsNone(w._usb_entry(root, "3-9"))       # a name that is not there at all
            self.assertIsNotNone(w._usb_entry(root, "3-7"))

    def test_a_device_going_away_is_not_a_half_device(self):
        # Mid-unplug the attributes vanish one by one.  What is left must be
        # "no device" for that tick, never a device on bus 0 that the VM
        # "cannot see" (which showed as "restart Windows" for a moment).
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "3-7", idVendor="1a86", idProduct="7523", bDeviceClass="ff", product="going")
            self.assertIsNone(w._usb_entry(root, "3-7"))
            d = self.make(root, "3-8", idVendor="1a86", idProduct="7524", bDeviceClass="ff", product="ok", busnum="3", devnum="9")
            # An interface whose driver link breaks under us is skipped, not fatal.
            os.makedirs(os.path.join(d, "3-8:1.0")); os.symlink("/nonexistent/drivers/x", os.path.join(d, "3-8:1.0", "driver"))
            e = w._usb_entry(root, "3-8")
            self.assertEqual((e["bus"], e["dev"], e["drivers"]), (3, 9, ["x"]))

    def test_store_survives_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            for text in ("", "[]", "{\"assigned\": 3}", "{\"assigned\": {\"1a86:7523\": \"x\", \"zz\": {}}}", "not json"):
                with open(p, "w") as f:
                    f.write(text)
                st = w.Store(p)
                self.assertEqual(st.assigned, {})
                # Not our shape at all: unusable (and left for a good list to
                # overwrite).  Our shape with garbage entries: just empty.
                self.assertEqual(st.unusable, "1a86" not in text, text)
                self.assertTrue(os.path.exists(p))
            with open(p, "w") as f:
                f.write("{\"assigned\": {\"1a86:7523\": {\"name\": \"dongle\"}}}")
            st = w.Store(p)
            self.assertEqual(list(st.assigned), ["1a86:7523"])
            self.assertFalse(st.unusable)
            os.unlink(p)
            self.assertFalse(w.Store(p).unusable)             # no file at all is a fresh start
            # Entries of the right shape but wrong content are made harmless,
            # not passed on to the panel and the sort in state_message.
            with open(p, "w") as f:
                json.dump({"assigned": {"1a86:7523": {"name": 42, "added": "x"}, "0403:6001": {"name": "\x00ok\n", "added": 5},
                                        "zzzz:0000": {"name": "no"}, "1111:2222": "not a dict"}}, f)
            st = w.Store(p)
            self.assertEqual(st.assigned, {"1a86:7523": {"name": "1a86:7523", "added": 0.0}, "0403:6001": {"name": "ok", "added": 5.0}})

    def test_store_remembers_a_failed_save(self):
        with tempfile.TemporaryDirectory() as d:
            st = w.Store(os.path.join(d, "state.json"))
            st.assigned["1a86:7523"] = {"name": "x", "added": 0}
            self.assertTrue(st.save()); self.assertFalse(st.unsaved)
            os.chmod(d, 0o500)
            try:
                if os.geteuid() != 0:
                    self.assertFalse(st.save()); self.assertTrue(st.unsaved)
            finally:
                os.chmod(d, 0o700)
            self.assertTrue(st.save()); self.assertFalse(st.unsaved)

    def test_real_sysfs_does_not_crash(self):
        devs = w.enumerate_usb("/sys/bus/usb/devices")
        for e in devs.values():
            self.assertRegex(e["key"], r"^[0-9a-f]{4}:[0-9a-f]{4}$")

class Launcher(unittest.TestCase):
    def daemon(self, d, conf_text=None):
        conf = os.path.join(d, "winplug.conf")
        if conf_text is not None:
            with open(conf, "w") as f:
                f.write(conf_text)
        os.environ["WINPLUG_NO_UDEV"] = "1"
        cfg = w.Config(conf)
        cfg.socket = os.path.join(d, "run", "winplug.sock")
        cfg.state = os.path.join(d, "state.json")
        dm = w.Daemon(cfg)
        self.addCleanup(os.close, dm.wake_r)
        self.addCleanup(os.close, dm.wake_w)
        return cfg, dm

    def client(self, dm):
        """A connected client as the daemon sees it, and our end of it."""
        c, peer = socket.socketpair()
        dm.clients[c] = {"buf": b""}
        self.addCleanup(c.close)
        self.addCleanup(peer.close)
        peer.settimeout(2)
        return c, peer

    def replies(self, peer):
        return [m for m in (json.loads(l) for l in peer.recv(65536).decode().split("\n") if l) if m.get("type") == "reply"]

    def test_tool_trusted(self):
        self.assertTrue(w.tool_trusted("/usr/bin/env"))       # root-owned, 0755, in root-owned dirs
        with tempfile.NamedTemporaryFile(delete=False) as f:
            mine = f.name
        os.chmod(mine, 0o755)
        self.assertFalse(w.tool_trusted(mine))                 # ours: never run as root
        os.unlink(mine)
        self.assertFalse(w.tool_trusted("/nonexistent/omarchy-windows-vm"))

    def test_job_argv(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            cfg.uid = 1000
            cfg.vm_runner = "direct"
            argv = dm.job_argv("up")
            self.assertEqual(argv[:2], ["env", "-i"])
            self.assertIn("PKEXEC_UID=1000", argv)
            self.assertEqual(argv[-3:], ["/usr/bin/omarchy-windows-vm", "__priv", "up"])
            cfg.vm_runner = "systemd-run"
            argv = dm.job_argv("down")
            self.assertEqual(argv[0], "systemd-run")
            self.assertIn("--wait", argv); self.assertIn("--pipe", argv)
            self.assertIn("--setenv=PKEXEC_UID=1000", argv)
            self.assertIn("RuntimeMaxSec=%d" % w.VM_JOB_TIMEOUT["down"], argv)
            self.assertEqual(argv[-3:], ["/usr/bin/omarchy-windows-vm", "__priv", "down"])

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_write_autostart(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d, "[winplug]\nuser = someone\n# a comment\nautostart = false\nusb_bus = xhci.0\n")
            self.assertFalse(cfg.autostart)
            self.assertEqual(dm.write_autostart(True), "")
            text = self.read(cfg.path)
            self.assertIn("autostart = true\n", text)
            self.assertIn("user = someone\n", text)
            self.assertIn("# a comment\n", text)
            self.assertNotIn("autostart = false", text)
            self.assertTrue(cfg.autostart)
            self.assertTrue(w.Config(cfg.path).autostart)
            # Without the key it goes right under the section header.
            with open(cfg.path, "w") as f:
                f.write("[winplug]\nuser = someone\n")
            self.assertEqual(dm.write_autostart(True), "")
            self.assertEqual(self.read(cfg.path), "[winplug]\nautostart = true\nuser = someone\n")
            # No config file at all.
            os.unlink(cfg.path)
            self.assertEqual(dm.write_autostart(False), "")
            self.assertEqual(self.read(cfg.path), "[winplug]\nautostart = false\n")
            self.assertFalse(os.path.exists(cfg.path + ".winplug.tmp"))

    def test_same_job_twice_answers_both(self):
        # Two "start" requests while one start is under way (autostart plus a
        # click, or two clicks): both get the one job's answer, nobody gets
        # "already being started".
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            cfg.uid = os.getuid(); cfg.vm_runner = "direct"; cfg.vm_tool = "/usr/bin/true"
            cfg.container_pid_override = "2147483647"; cfg.compose = os.path.join(d, "none.yml")
            dm.launcher_ok = True
            a, a_peer = self.client(dm); b, b_peer = self.client(dm)
            self.assertEqual(dm.start_job("up", a, 1), "")
            self.assertEqual(dm.start_job("up", b, 2), "")
            self.assertEqual(dm.start_job("down", b, 3), "Windows is already being started")
            dm.job["thread"].join(10)
            dm.finish_job()
            self.assertIsNone(dm.job)
            for peer, rid in ((a_peer, 1), (b_peer, 2)):
                self.assertEqual([(m["ok"], m["id"]) for m in self.replies(peer)], [(True, rid)])

    def test_handle_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            c, peer = self.client(dm)
            dm.store.save = lambda: (_ for _ in ()).throw(RuntimeError("disk on fire"))
            dm.handle(c, {"cmd": "attach", "key": "1a86:7523", "id": 7})
            m = self.replies(peer)[0]
            self.assertEqual((m["ok"], m["id"]), (False, 7))
            self.assertIn("disk on fire", m["error"])
            # What the daemon believes and what it reports still agree.
            snap = {e["key"]: e["assigned"] for e in dm.state_message()["devices"]}
            self.assertEqual(snap.get("1a86:7523", False), "1a86:7523" in dm.store.assigned)

    def test_refused_add_is_retried_slowly_and_forgotten_on_detach(self):
        # After QEMU refuses a device, the daemon waits ADD_RETRY_SECONDS
        # before asking again -- but a take-back or a replug starts afresh.
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            now = 1000.0
            dm.note_add_failure("1a86:7523", "USB device not found", now)
            until, err = dm.add_failed["1a86:7523"]
            self.assertEqual((until, err), (now + w.ADD_RETRY_SECONDS, "USB device not found"))
            dm.store.assigned["1a86:7523"] = {"name": "x", "added": 0}
            c, peer = self.client(dm)
            dm.handle(c, {"cmd": "detach", "key": "1a86:7523"})
            self.assertNotIn("1a86:7523", dm.add_failed)

    def fake_monitor(self, dm, in_guest):
        """A monitor that lists `in_guest` (qemu ids) and records commands."""
        sent = []
        def hmp(path, command, timeout=5.0):
            sent.append(command)
            if command == "info usb":
                return "\n".join("  Device 0.%d, Port 1, Speed 12 Mb/s, Product x, ID: %s" % (i, q) for i, q in enumerate(in_guest, 1))
            return ""
        self.addCleanup(setattr, w, "hmp", w.hmp)
        w.hmp = hmp
        cfg = dm.cfg
        cfg.container_pid_override = str(os.getpid())
        cfg.monitor_override = os.path.join(os.path.dirname(cfg.state), "monitor.sock")
        open(cfg.monitor_override, "w").close()
        dm.vm.refresh(time.monotonic())
        self.assertTrue(dm.vm.running and dm.vm.monitor)
        return sent

    def test_unusable_state_adopts_what_is_in_windows(self):
        # A state file that cannot be read must never end in device_del of
        # everything that is in Windows (it may be mid-flash): the devices
        # found there come back onto the list instead.
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            with open(cfg.state, "w") as f:
                f.write("{corrupt")
            dm.store.load()
            self.assertTrue(dm.store.unusable)
            sent = self.fake_monitor(dm, ["winplug-1a86-7523", "winplug-0403-6001", "usb-tablet"])
            dm.reconcile()
            self.assertFalse([c for c in sent if c.startswith("device_del")], sent)
            self.assertEqual(set(dm.store.assigned), {"1a86:7523", "0403:6001"})
            self.assertEqual(dm.status["1a86:7523"], "attached")
            self.assertFalse(dm.store.unusable)                 # a good list was written
            self.assertEqual(set(w.Store(cfg.state).assigned), {"1a86:7523", "0403:6001"})
            # From now on the audit is back to normal: an unassign takes effect.
            dm.store.assigned.pop("0403:6001"); dm.store.save()
            dm.last_poll = 0; dm.reconcile()
            self.assertIn("device_del winplug-0403-6001", sent)

    def test_stale_audit_waits_while_the_list_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            sent = self.fake_monitor(dm, ["winplug-1a86-7523"])
            dm.store.unsaved = True
            dm.reconcile()
            self.assertFalse([c for c in sent if c.startswith("device_del")], sent)
            dm.store.unsaved = False
            dm.last_poll = 0; dm.reconcile()
            self.assertIn("device_del winplug-1a86-7523", sent)

    def test_snapshot_never_takes_the_daemon_down(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            dm.store.assigned["1a86:7523"] = {"name": None, "added": 0}     # not what Store.load would give, but never fatal
            self.assertEqual([e["name"] for e in dm.state_message()["devices"] if e["key"] == "1a86:7523"], ["1a86:7523"])
            dm.state_message = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            lst = socket.socket(socket.AF_UNIX); lst.bind(os.path.join(d, "l.sock")); lst.listen(1); lst.setblocking(False)
            self.addCleanup(lst.close)
            dm.listener = lst
            peer = socket.socket(socket.AF_UNIX); peer.connect(os.path.join(d, "l.sock")); self.addCleanup(peer.close)
            dm.accept()                                          # must not raise
            self.assertEqual(len(dm.clients), 1)
            for c in list(dm.clients): dm.drop(c)

    def test_job_worker_crash_still_answers(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            cfg.uid = os.getuid(); cfg.vm_runner = "direct"; cfg.vm_tool = "/usr/bin/true"
            cfg.container_pid_override = "2147483647"; cfg.compose = os.path.join(d, "none.yml")
            dm.launcher_ok = True
            self.addCleanup(setattr, w.subprocess, "run", w.subprocess.run)
            w.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no fork today"))
            c, peer = self.client(dm)
            self.assertEqual(dm.start_job("up", c, 1), "")
            dm.job["thread"].join(10)
            dm.finish_job()
            self.assertIsNone(dm.job)
            m = self.replies(peer)[0]
            self.assertEqual((m["ok"], m["id"]), (False, 1)); self.assertIn("no fork today", m["error"])

    def test_job_watchdog_and_dropped_waiter(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            cfg.container_pid_override = "2147483647"; cfg.compose = os.path.join(d, "none.yml")
            gone, gone_peer = self.client(dm)
            here, here_peer = self.client(dm)
            t = threading.Thread(target=lambda: None); t.start(); t.join()
            dm.job = {"kind": "up", "waiters": [(gone, 1), (here, 2)], "result": None, "started": time.monotonic() - 1, "thread": t}
            dm.check_job(time.monotonic())
            self.assertIsNotNone(dm.job)                        # not yet
            dm.drop(gone)
            dm.check_job(time.monotonic() + w.VM_JOB_TIMEOUT["up"] + 61)
            self.assertIsNone(dm.job)
            m = self.replies(here_peer)[0]
            self.assertEqual((m["ok"], m["id"]), (False, 2)); self.assertIn("gave up", m["error"])

    def test_tick_survives_a_bad_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            dm.vm.refresh = lambda now: (_ for _ in ()).throw(RuntimeError("docker exploded"))
            dm.tick()                                            # logged, not raised

    def test_refusal_restarts_the_no_access_clock(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            dm.unattached_since["1a86:7523"] = 1.0
            dm.note_add_failure("1a86:7523", "refused", 100.0)
            self.assertEqual(dm.unattached_since["1a86:7523"], 100.0)

    def test_autostart_means_at_boot(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d, "[winplug]\nautostart = true\n")
            dm.launcher_ok = True
            os.makedirs(os.path.dirname(dm.autostart_marker))    # /run/winplug, in production
            self.addCleanup(setattr, w, "boot_age", w.boot_age)
            w.boot_age = lambda: 30.0
            dm.plan_autostart(); self.assertGreater(dm.autostart_at, 0)
            dm.autostart_at = 0.0
            w.boot_age = lambda: 3 * 3600.0                     # an upgrade this afternoon
            dm.plan_autostart(); self.assertEqual(dm.autostart_at, 0.0)
            w.boot_age = lambda: 30.0
            self.assertEqual(dm.write_autostart(True), "")      # switched on now: this boot is done
            dm.plan_autostart(); self.assertEqual(dm.autostart_at, 0.0)
            self.assertTrue(os.path.exists(dm.autostart_marker))

    def test_compose_unpatch_keeps_other_rules(self):
        awk = os.path.join(here, "..", "system", "compose-unpatch.awk")
        both = "services:\n  w:\n    volumes:\n      - /a:/a\n      - /dev/bus/usb:/dev/bus/usb\n    device_cgroup_rules:\n      - \"c 189:* rmw\"\n"
        run = lambda text: subprocess.run(["awk", "-f", awk], input=text, capture_output=True, text=True).stdout
        self.assertEqual(run(both + "    restart: \"no\"\n"), "services:\n  w:\n    volumes:\n      - /a:/a\n    restart: \"no\"\n")
        self.assertEqual(run(both + "      - \"c 4:* rmw\"\n    restart: \"no\"\n"),
                         "services:\n  w:\n    volumes:\n      - /a:/a\n    device_cgroup_rules:\n      - \"c 4:* rmw\"\n    restart: \"no\"\n")
        self.assertEqual(run(both), "services:\n  w:\n    volumes:\n      - /a:/a\n")

    def test_state_reports_job_and_autostart(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d, "[winplug]\nautostart = true\n")
            cfg.compose = os.path.join(d, "compose.yml")
            open(cfg.compose, "w").close()
            dm.vm.installed = True
            st = dm.state_message()["vm"]
            self.assertTrue(st["autostart"]); self.assertEqual(st["job"], "")
            self.assertIn("launcher", st)
            dm.job = {"kind": "up"}
            st = dm.state_message()["vm"]
            self.assertEqual((st["status"], st["job"]), ("starting", "up"))
            dm.job = {"kind": "down"}
            self.assertEqual(dm.state_message()["vm"]["status"], "stopping")

class Rdp(unittest.TestCase):
    def test_session_ended_by(self):
        for rc in (0, 2, 11, 12):
            self.assertEqual(cli.session_ended_by(rc), "user")
        for rc in (1, 3, 5, 10):
            self.assertEqual(cli.session_ended_by(rc), "windows")
        for rc in (131, 141, 147, 255):
            self.assertEqual(cli.session_ended_by(rc), "nobody")
        # Reopen after a reboot-like end, never after a takeover or a refusal.
        for rc in (5, 7, 8, 9, 10):
            self.assertIn(rc, cli.FINAL_RDP_EXITS)
        for rc in (1, 3, 4, 6, 12):
            self.assertNotIn(rc, cli.FINAL_RDP_EXITS)

    def sessions(self, exits, answers=True, status="running"):
        """Run the session loop over a scripted list of xfreerdp exit codes;
        returns (why, notifications, number of sessions)."""
        said = []
        for name in ("run_rdp", "rdp_answers", "vm_now", "wait_rdp", "_sleep"):
            self.addCleanup(setattr, cli, name, getattr(cli, name))
        codes = list(exits)
        cli.run_rdp = lambda u, p: codes.pop(0)
        cli.rdp_answers = lambda timeout=2.0: answers
        cli.vm_now = lambda patience=0.0: {"status": status, "message": status}
        cli.wait_rdp = lambda timeout, every=1.0, tick=None: answers
        cli._sleep = lambda s: None
        say = lambda headline, body="", urgency="normal", notify=None: said.append((headline, notify))
        why = cli.run_sessions("u", "p", say)
        return why, said, len(exits) - len(codes)

    def test_sessions_end_with_the_user(self):
        self.assertEqual(self.sessions([12])[0], "user")
        self.assertEqual(self.sessions([0])[0], "user")

    def test_takeover_is_final_and_leaves_windows_alone(self):
        # Another client took the session (5): not reopened, and the caller
        # is told "final" so it never sends vm_down under the other client.
        why, said, n = self.sessions([5, 0])
        self.assertEqual((why, n), ("final", 1))
        self.assertEqual(said[-1][0], "Windows went to another client")
        why, said, n = self.sessions([7, 0])
        self.assertEqual((why, n), ("final", 1))

    def test_reboot_is_reopened_but_a_bouncing_windows_is_not(self):
        # A long session ended by Windows (a reboot) comes back; sessions
        # that die the moment they open, again and again, do not go on for ever.
        self.addCleanup(setattr, cli.time, "monotonic", cli.time.monotonic)
        clock = [0.0]
        def tick():
            clock[0] += 100.0            # every session "lasts" 100 s
            return clock[0]
        cli.time.monotonic = tick
        self.assertEqual(self.sessions([1, 1, 1, 1, 1, 1, 1, 0])[0], "user")
        cli.time.monotonic = lambda: 0.0 # every session lasts 0 s
        why, said, n = self.sessions([1] * 20)
        self.assertEqual((why, n), ("bounced", cli.MAX_SHORT_SESSIONS))
        self.assertEqual(said[-1][0], "Windows keeps ending the session")
        # And one bubble for the spell, not one per try.
        self.assertEqual([s for s in said if s[0] == "Windows closed the session" and s[1] is None].__len__(), 1)

    def test_windows_that_does_not_come_back_is_gone(self):
        self.assertEqual(self.sessions([1], answers=False, status="off")[0], "gone")

    def test_rdp_answers_only_for_a_real_listener(self):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(2)
        cli.RDP_HOST, cli.RDP_PORT = "127.0.0.1", srv.getsockname()[1]
        def serve(reply):
            c, _ = srv.accept(); c.recv(64)
            if reply: c.sendall(reply)
            c.close()
        # Accepts and hangs up: docker's proxy with nothing behind it.
        t = threading.Thread(target=serve, args=(b"",)); t.start()
        self.assertFalse(cli.rdp_answers(timeout=2)); t.join()
        # An X.224 Connection Confirm: an RDP listener.
        t = threading.Thread(target=serve, args=(bytes.fromhex("030000130ed000001234000200080002000000"),)); t.start()
        self.assertTrue(cli.rdp_answers(timeout=2)); t.join()
        srv.close()
        self.assertFalse(cli.rdp_answers(timeout=1))            # nothing listening

    def test_x224_request_is_well_formed(self):
        req = cli.X224_CONNECT
        self.assertEqual(len(req), 19)
        self.assertEqual(req[:4], b"\x03\x00\x00\x13")          # TPKT, length 19
        self.assertEqual(req[4], 14); self.assertEqual(req[5], 0xE0)   # X.224 CR
        self.assertEqual(req[11], 1)                            # RDP_NEG_REQ

class SingleSession(unittest.TestCase):
    def test_launch_lock_is_single_holder(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d)
        before = os.environ.get("XDG_RUNTIME_DIR")
        self.addCleanup(lambda: os.environ.update({"XDG_RUNTIME_DIR": before}) if before is not None else os.environ.pop("XDG_RUNTIME_DIR", None))
        os.environ["XDG_RUNTIME_DIR"] = d
        cli._LAUNCH_LOCK_FD = None
        fd1 = cli.acquire_launch_lock()
        self.assertIsNotNone(fd1)
        self.assertIsNone(cli.acquire_launch_lock())            # a second launch is blocked
        os.close(fd1)                                           # first launch exits
        fd2 = cli.acquire_launch_lock()                         # now free again
        self.assertIsNotNone(fd2)
        os.close(fd2)
        # No lock file possible: carry on rather than pretend someone has it.
        os.environ["XDG_RUNTIME_DIR"] = os.path.join(d, "nonexistent")
        self.assertEqual(cli.acquire_launch_lock(), -1)

    def test_rdp_window_open_detects_a_client(self):
        cli.RDP_HOST, cli.RDP_PORT = "127.0.0.1", 33899
        self.assertFalse(cli.rdp_window_open())
        # A stand-in whose argv looks like an xfreerdp connected to this VM.
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              "xfreerdp3", "/v:127.0.0.1:33899"])
        try:
            seen = any(cli.rdp_window_open() or time.sleep(0.05) for _ in range(40))
            self.assertTrue(seen)
        finally:
            p.terminate(); p.wait()
        self.assertFalse(cli.rdp_window_open())

if __name__ == "__main__":
    unittest.main(verbosity=1)
