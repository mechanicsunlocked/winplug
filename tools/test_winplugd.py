#!/usr/bin/env python3
"""Unit tests for the pure parts of winplugd (no root, no VM)."""
import os, sys, tempfile, unittest, importlib.util, stat

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("winplugd", os.path.join(here, "..", "system", "winplugd.py"))
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

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

if __name__ == "__main__":
    unittest.main(verbosity=1)
