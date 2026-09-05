#!/usr/bin/env python3
"""Unit tests for the pure parts of winplugd (no root, no VM)."""
import os, sys, json, tempfile, unittest, importlib.util, importlib.machinery, stat, socket, threading, subprocess, time

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
            bad = self.make(root, "3-8", idVendor="0403", idProduct="6001", bDeviceClass="00", product="gone", busnum="3", devnum="10")
            os.chmod(bad, 0)
            try:
                devs = w.enumerate_usb(root)
            finally:
                os.chmod(bad, 0o755)
            self.assertEqual(set(devs), {"1a86:7523"})
            self.assertIsNone(w._usb_entry(root, "3-9"))       # a name that is not there at all
            self.assertIsNotNone(w._usb_entry(root, "3-7"))

    def test_store_survives_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            for text in ("", "[]", "{\"assigned\": 3}", "{\"assigned\": {\"1a86:7523\": \"x\", \"zz\": {}}}", "not json"):
                with open(p, "w") as f:
                    f.write(text)
                self.assertEqual(w.Store(p).assigned, {})
            with open(p, "w") as f:
                f.write("{\"assigned\": {\"1a86:7523\": {\"name\": \"dongle\"}}}")
            self.assertEqual(list(w.Store(p).assigned), ["1a86:7523"])

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
        return cfg, w.Daemon(cfg)

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
            a, a_peer = socket.socketpair(); b, b_peer = socket.socketpair()
            dm.clients[a] = {"buf": b""}; dm.clients[b] = {"buf": b""}
            self.assertEqual(dm.start_job("up", a, 1), "")
            self.assertEqual(dm.start_job("up", b, 2), "")
            self.assertEqual(dm.start_job("down", b, 3), "Windows is already being started")
            dm.job["thread"].join(10)
            dm.finish_job()
            self.assertIsNone(dm.job)
            for peer, rid in ((a_peer, 1), (b_peer, 2)):
                peer.settimeout(2)
                lines = [json.loads(l) for l in peer.recv(65536).decode().split("\n") if l]
                replies = [m for m in lines if m.get("type") == "reply"]
                self.assertEqual([(m["ok"], m["id"]) for m in replies], [(True, rid)])
            for s in (a, b, a_peer, b_peer):
                s.close()
            os.close(dm.wake_r); os.close(dm.wake_w)

    def test_handle_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, dm = self.daemon(d)
            c, peer = socket.socketpair(); dm.clients[c] = {"buf": b""}
            dm.store.save = lambda: (_ for _ in ()).throw(RuntimeError("disk on fire"))
            dm.handle(c, {"cmd": "attach", "key": "1a86:7523", "id": 7})
            peer.settimeout(2)
            m = json.loads(peer.recv(65536).decode().split("\n")[0])
            self.assertEqual((m["ok"], m["id"]), (False, 7))
            self.assertIn("disk on fire", m["error"])
            c.close(); peer.close(); os.close(dm.wake_r); os.close(dm.wake_w)

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
        for rc in (0, 2, 11):
            self.assertEqual(cli.session_ended_by(rc), "user")
        for rc in (1, 3, 5, 10):
            self.assertEqual(cli.session_ended_by(rc), "windows")
        for rc in (131, 141, 147, 255):
            self.assertEqual(cli.session_ended_by(rc), "nobody")
        # Reopen after a reboot-like end, never after a takeover or a refusal.
        for rc in (1, 3, 4, 6, 12):
            self.assertTrue(cli.reopen_after(rc))
        for rc in (0, 2, 11, 5, 7, 8, 9, 10, 131):
            self.assertFalse(cli.reopen_after(rc))

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
        d = tempfile.mkdtemp()
        os.environ["XDG_RUNTIME_DIR"] = d
        cli._LAUNCH_LOCK_FD = None
        fd1 = cli.acquire_launch_lock()
        self.assertIsNotNone(fd1)
        self.assertIsNone(cli.acquire_launch_lock())            # a second launch is blocked
        os.close(fd1)                                           # first launch exits
        fd2 = cli.acquire_launch_lock()                         # now free again
        self.assertIsNotNone(fd2)
        os.close(fd2)

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
