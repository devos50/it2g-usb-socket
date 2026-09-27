"""
Regression smoke test for the USB link: boots the emulator in DFU mode and
walks DFU -> iBSS -> iBEC over USB, checking each step. Takes a few seconds.

  python3 smoke_test.py [--qemu ...] [--firmware ...]

Exits with 0 when every step passes.
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

from main import dfu_upload
from recovery import upload
from usb_link import USBLink, USBNoDevice, USBStall, USBTimeout

DOCS = os.path.expanduser("~/Documents")
DFU_PID = 0x1227
RECOVERY_PID = 0x1281


class StepFailed(Exception):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise StepFailed("timed out waiting for %s" % what)


class Emulator:
    def __init__(self, args, workdir):
        self.port = free_port()
        self.serial_log = os.path.join(workdir, "serial.log")
        cmd = [
            args.qemu, "-M",
            "iPod-Touch,bootrom=%s,nand=%s,nor=%s,usb-chardev=usb,dfu=on" % (args.bootrom, args.nand, args.nor),
            "-cpu", "max", "-m", "2G", "-display", "none", "-monitor", "none",
            "-serial", "file:" + self.serial_log,
            "-chardev", "socket,id=usb,host=127.0.0.1,port=%d,server=on,wait=off,nodelay=on" % self.port,
        ]
        self.log = open(os.path.join(workdir, "qemu.log"), "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)

    def serial(self):
        try:
            with open(self.serial_log, errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def connect(self):
        link = None

        def try_connect():
            nonlocal link
            if self.proc.poll() is not None:
                raise StepFailed("QEMU exited with %d" % self.proc.returncode)
            try:
                link = USBLink("127.0.0.1", self.port)
                return True
            except ConnectionRefusedError:
                return False

        wait_for(try_connect, 10, "the USB link socket")
        return link

    def stop(self):
        self.proc.kill()
        self.proc.wait()
        self.log.close()


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
    emu = Emulator(args, workdir)
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

    start = time.monotonic()
    failed = False
    try:
        link = emu.connect()
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
        if link:
            link.close()
        emu.stop()

    print("%s in %.1f s (logs in %s)" % ("FAILED" if failed else "PASSED", time.monotonic() - start, workdir))
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qemu", default=os.path.join(DOCS, "qemu-ios/build/qemu-system-arm"))
    parser.add_argument("--bootrom", default=os.path.join(DOCS, "ipod_touch_2g_emulation/bootrom_240_4"))
    parser.add_argument("--nand", default=os.path.join(DOCS, "generate_nand_it2g/nand"))
    parser.add_argument("--nor", default=os.path.join(DOCS, "generate_nor_it2g/nor.bin"))
    parser.add_argument("--firmware", default=os.path.expanduser(
        "~/Downloads/iPod2,1_2.1.1_5F138_Restore/Firmware/dfu"))
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
