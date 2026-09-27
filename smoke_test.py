"""
Regression smoke test for the USB link: boots the emulator in DFU mode and
walks DFU -> iBSS -> iBEC over USB, checking each step. Takes a few seconds.

  python3 smoke_test.py [--qemu ...] [--firmware ...]

With --ios it instead boots iOS from a clone of the NAND (so the NAND is not
modified) and checks that the device appears through the usbmuxd bridge and
answers lockdownd. That takes a few minutes.

Exits with 0 when every step passes.
"""
import argparse
import os
import plistlib
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

from emulator import Emulator, EmulatorError, add_arguments, wait_for
from main import dfu_upload
from recovery import upload
from usb_link import USBNoDevice, USBStall, USBTimeout

DFU_PID = 0x1227
RECOVERY_PID = 0x1281
IOS_BOOT_TIMEOUT = 600

StepFailed = EmulatorError


def enumerate_device(link, expected_pid):
    desc = link.wait_for_device(timeout=20)
    pid = struct.unpack_from("<H", desc, 10)[0]
    if pid != expected_pid:
        raise StepFailed("expected product 0x%04x, got 0x%04x" % (expected_pid, pid))
    return link.get_string(desc[16])


def command(link, text, timeout=5):
    link.ctrl_transfer(0x40, 0, 0, 0, text.encode() + b"\0", timeout=timeout)


class Console:
    """The USB serial console. Its output only arrives while an IN transfer is
    pending, so one stays pending while commands go over EP0."""

    def __init__(self, link):
        link.ctrl_transfer(0x01, 0x0B, 1, 1)  # SET_INTERFACE 1, alternate setting 1
        self.link = link
        self.pending = link.submit_bulk_read(0x01, 0x200)

    def expect(self, command_text, expected, timeout=5):
        output = b""
        command(self.link, command_text)
        deadline = time.monotonic() + timeout
        while expected.encode() not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StepFailed("console never printed %r, got %r" % (expected, output[-200:]))
            if not self.pending.wait_done(remaining):
                continue
            output += self.link.wait(self.pending)
            self.pending = self.link.submit_bulk_read(0x01, 0x200)


def run(args):
    ibss = os.path.join(args.firmware, "iBSS.n72ap.RELEASE.dfu")
    ibec = os.path.join(args.firmware, "iBEC.n72ap.RELEASE.dfu")
    with open(ibss, "rb") as f:
        ibss_data = f.read()
    with open(ibec, "rb") as f:
        ibec_data = f.read()

    workdir = tempfile.mkdtemp(prefix="it2g-smoke-")
    emu = Emulator(args, workdir, dfu=True)
    link = None
    state = {}

    def step_dfu_enumerates():
        serial = enumerate_device(link, DFU_PID)
        if "SRTG:[iBoot-240.4]" not in serial:
            raise StepFailed("unexpected DFU serial %r" % serial)

    def step_dfu_upload_ibss():
        dfu_upload(link, ibss_data)
        wait_for(lambda: "Entering recovery mode" in emu.serial(), 20, "iBSS to enter recovery mode")

    def step_ibss_enumerates():
        serial = enumerate_device(link, RECOVERY_PID)
        if "SRNM:" not in serial:
            raise StepFailed("unexpected recovery serial %r" % serial)
        link.ctrl_transfer(0x00, 0x09, 1, 0)  # SET_CONFIGURATION

    def step_ibss_commands():
        command(link, "setenv smoketest 1")
        try:
            command(link, "bogus")
        except USBStall:
            return
        raise StepFailed("an unknown command did not stall")

    def step_ibss_console():
        state["console"] = Console(link)
        state["console"].expect("printenv smoketest", "smoketest = '1'")

    def step_upload_ibec():
        upload(link, ibec_data)
        state["console"].expect("md 0x09000000 0x10", "496d6733")  # IMG3 magic at loadaddr

    def step_go_ibec():
        try:
            command(link, "go", timeout=5)
        except (USBNoDevice, USBTimeout):
            pass
        wait_for(lambda: "iBEC for n72ap" in emu.serial(), 20, "iBEC to start")

    def step_ibec_enumerates():
        enumerate_device(link, RECOVERY_PID)
        link.ctrl_transfer(0x00, 0x09, 1, 0)
        Console(link).expect("printenv auto-boot", "auto-boot")

    steps = [
        ("DFU enumerates", step_dfu_enumerates),
        ("DFU upload of iBSS", step_dfu_upload_ibss),
        ("iBSS enumerates in recovery mode", step_ibss_enumerates),
        ("iBSS commands (ok and stall)", step_ibss_commands),
        ("iBSS serial console", step_ibss_console),
        ("recovery upload of iBEC", step_upload_ibec),
        ("go boots iBEC", step_go_ibec),
        ("iBEC enumerates, console works", step_ibec_enumerates),
    ]

    try:
        link = emu.connect()
    except EmulatorError as e:
        emu.stop()
        print("FAIL  %-34s %s" % ("USB link connects", e))
        return 1
    try:
        return run_steps(emu, steps, workdir)
    finally:
        link.close()


def run_steps(emu, steps, workdir):
    start = time.monotonic()
    failed = False
    try:
        for name, fn in steps:
            step_start = time.monotonic()
            try:
                fn()
            except Exception as e:
                print("FAIL  %-34s %s: %s" % (name, type(e).__name__, e))
                failed = True
                break
            print("ok    %-34s %.2f s" % (name, time.monotonic() - step_start))
    finally:
        emu.stop()

    print("%s in %.1f s (logs in %s)" % ("FAILED" if failed else "PASSED", time.monotonic() - start, workdir))
    return 1 if failed else 0


def run_ios(args):
    """Boots iOS from a clone of the NAND and checks that it appears through
    the usbmuxd bridge and answers lockdownd. Takes a few minutes."""
    from usbmuxd import start_bridge

    workdir = tempfile.mkdtemp(prefix="it2g-smoke-ios-")
    emu = Emulator(args, workdir, scratch_nand=True)
    listen = "127.0.0.1:%d" % free_listen_port()
    devices = start_bridge(listen, port=emu.port, pair_records=os.path.join(workdir, "pair_records"))
    env = dict(os.environ, USBMUXD_SOCKET_ADDRESS=listen)

    def step_attached():
        deadline = time.monotonic() + IOS_BOOT_TIMEOUT
        while not devices.attached.wait(1):
            emu.check_running()
            if time.monotonic() > deadline:
                raise StepFailed("the device did not attach within %d s" % IOS_BOOT_TIMEOUT)

    def step_querytype():
        device_id, _, _ = devices.current()
        reply = muxd_request(listen, {"MessageType": "Connect", "DeviceID": device_id,
                                      "PortNumber": socket.htons(62078)}, tunnel=True)
        if reply.get("Type") != "com.apple.mobile.lockdown":
            raise StepFailed("unexpected QueryType reply %r" % reply)

    def step_ideviceinfo():
        if not shutil.which("ideviceinfo"):
            print("      (ideviceinfo not installed, skipped)")
            return
        out = subprocess.run(["ideviceinfo", "-s", "-k", "DeviceClass"], env=env,
                             capture_output=True, text=True, timeout=30)
        if out.stdout.strip() != "iPod":
            raise StepFailed("ideviceinfo printed %r %r" % (out.stdout, out.stderr))

    steps = [
        ("iOS boots and attaches over usbmux", step_attached),
        ("lockdownd QueryType via the bridge", step_querytype),
        ("ideviceinfo -s via the bridge", step_ideviceinfo),
    ]
    return run_steps(emu, steps, workdir)


def free_listen_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def muxd_request(listen, message, tunnel=False):
    """Sends one usbmuxd request. With tunnel, the request is a Connect to
    lockdownd and the lockdownd QueryType reply is returned."""
    header = struct.Struct("<IIII")
    host, _, port = listen.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=30) as s:
        body = plistlib.dumps(message)
        s.sendall(header.pack(header.size + len(body), 1, 8, 1) + body)
        length = header.unpack(recv_exact(s, header.size))[0]
        reply = plistlib.loads(recv_exact(s, length - header.size))
        if not tunnel:
            return reply
        if reply.get("Number") != 0:
            raise StepFailed("Connect failed: %r" % reply)
        body = plistlib.dumps({"Label": "smoke_test", "Request": "QueryType"})
        s.sendall(struct.pack(">I", len(body)) + body)
        length = struct.unpack(">I", recv_exact(s, 4))[0]
        return plistlib.loads(recv_exact(s, length))


def recv_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise StepFailed("connection closed")
        data += chunk
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    parser.add_argument("--firmware", default=os.path.expanduser(
        "~/Downloads/iPod2,1_2.1.1_5F138_Restore/Firmware/dfu"))
    parser.add_argument("--ios", action="store_true",
                        help="instead, boot iOS from a clone of the NAND and check usbmux (takes minutes)")
    args = parser.parse_args()
    return run_ios(args) if args.ios else run(args)


if __name__ == "__main__":
    sys.exit(main())
