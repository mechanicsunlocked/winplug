#!/usr/bin/env python3
"""Unit tests for the pure parts of winplugd (no root, no VM)."""
import os, sys, tempfile, unittest, importlib.util, importlib.machinery, stat, socket, threading

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

class Hmp(unittest.TestCase):
    def test_parse_info_usb(self):
        txt = ("  Device 0.1, Port 1, Speed 480 Mb/s, Product QEMU USB Tablet, ID: usb-tablet\n"
               "  Device 0.2, Port 2, Speed 12 Mb/s, Product USB2.0-Serial, ID: winplug-1a86-7523\n")
        self.assertEqual(w.parse_info_usb(txt), {"usb-tablet", "winplug-1a86-7523"})
        self.assertEqual(w.parse_info_usb(""), set())

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

if __name__ == "__main__":
    unittest.main(verbosity=1)
