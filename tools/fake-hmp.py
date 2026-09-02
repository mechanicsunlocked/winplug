#!/usr/bin/env python3
"""A stand-in for QEMU's human monitor, for testing winplugd without a VM.

Speaks just enough HMP: banner + "(qemu) " prompt, echo of the command,
`info usb`, `device_add usb-host,...`, `device_del <id>`.  A usb-host device
counts as attached to the guest only while a matching device exists in the
fake host list, which is controlled through a side channel:

    fake-hmp.py <monitor.sock> <control.sock>

    echo 'plug 1a86:7523'   | nc -U control.sock
    echo 'unplug 1a86:7523' | nc -U control.sock
    echo 'noaccess 1'       | nc -U control.sock   # libusb_open fails: never attaches
    echo 'dump'             | nc -U control.sock

Handles one monitor client at a time, like QEMU."""
import os, re, select, socket, sys, json

mon_path, ctl_path = sys.argv[1], sys.argv[2]
for p in (mon_path, ctl_path):
    try: os.unlink(p)
    except OSError: pass
mon = socket.socket(socket.AF_UNIX); mon.bind(mon_path); mon.listen(4)
ctl = socket.socket(socket.AF_UNIX); ctl.bind(ctl_path); ctl.listen(4)

host = set()               # vid:pid present on the "host"
qdevs = {}                 # id -> {"vid","pid","bus"}
no_access = False
log = []

def attached_ids():
    if no_access: return []
    return [i for i, d in qdevs.items() if d["vid"] + ":" + d["pid"] in host]

def run(cmd):
    log.append(cmd)
    cmd = cmd.strip()
    if cmd == "info usb":
        out = ""
        for n, i in enumerate(attached_ids()):
            d = qdevs[i]
            out += "  Device 0.%d, Port %d, Speed 480 Mb/s, Product Fake %s:%s, ID: %s\n" % (n + 1, n + 1, d["vid"], d["pid"], i)
        return out
    m = re.fullmatch(r"device_add usb-host,vendorid=0x([0-9a-f]{4}),productid=0x([0-9a-f]{4}),id=([\w.-]+)(?:,bus=([\w.-]+))?", cmd)
    if m:
        vid, pid, i, bus = m.groups()
        if i in qdevs: return "Duplicate device ID '%s'\n" % i
        if bus and bus != "xhci.0": return "Bus '%s' not found\n" % bus
        qdevs[i] = {"vid": vid, "pid": pid, "bus": bus}
        return ""
    m = re.fullmatch(r"device_del ([\w.-]+)", cmd)
    if m:
        if m.group(1) not in qdevs: return "Device '%s' not found\n" % m.group(1)
        del qdevs[m.group(1)]
        return ""
    return "unknown command: '%s'\n" % cmd

def control(line):
    global no_access
    parts = line.split()
    if not parts: return ""
    if parts[0] == "plug": host.add(parts[1]); return "ok\n"
    if parts[0] == "unplug": host.discard(parts[1]); return "ok\n"
    if parts[0] == "noaccess": no_access = parts[1] == "1"; return "ok\n"
    if parts[0] == "dump": return json.dumps({"host": sorted(host), "qdevs": qdevs, "attached": attached_ids(), "log": log[-20:]}) + "\n"
    return "?\n"

client = None; buf = b""
while True:
    rl = [mon, ctl] + ([client] if client else [])
    r, _, _ = select.select(rl, [], [])
    for s in r:
        if s is ctl:
            c, _ = ctl.accept(); data = c.recv(4096).decode()
            c.sendall(control(data.strip()).encode()); c.close()
        elif s is mon:
            c, _ = mon.accept()
            if client:  # QEMU leaves extra clients waiting; we just queue one
                c.close(); continue
            client = c; buf = b""
            client.sendall(b"QEMU 10.0.0 monitor - type 'help' for more information\r\n(qemu) ")
        else:
            data = client.recv(4096)
            if not data:
                client.close(); client = None; continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode().strip()
                out = run(cmd).replace("\n", "\r\n")
                client.sendall((cmd + "\r\n" + out + "(qemu) ").encode())
