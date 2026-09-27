"""
Starts the iPod Touch 2G emulator with the USB link on a free local port.
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time

from usb_link import USBLink

DOCS = os.path.expanduser("~/Documents")
DEFAULT_QEMU = os.path.join(DOCS, "qemu-ios/build/qemu-system-arm")
DEFAULT_BOOTROM = os.path.join(DOCS, "ipod_touch_2g_emulation/bootrom_240_4")
DEFAULT_NAND = os.path.join(DOCS, "generate_nand_it2g/nand")
DEFAULT_NOR = os.path.join(DOCS, "generate_nor_it2g/nor.bin")


class EmulatorError(Exception):
    pass


def add_arguments(parser):
    parser.add_argument("--qemu", default=DEFAULT_QEMU)
    parser.add_argument("--bootrom", default=DEFAULT_BOOTROM)
    parser.add_argument("--nand", default=DEFAULT_NAND)
    parser.add_argument("--nor", default=DEFAULT_NOR)


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
    raise EmulatorError("timed out waiting for %s" % what)


class Emulator:
    """Runs QEMU with its serial port logged to <workdir>/serial.log.

    dfu: hold the force-DFU GPIO so the bootrom enters DFU mode.
    display: show the QEMU window instead of running headless.
    scratch_nand: boot a copy-on-write clone of the NAND, so the run does not
    modify it (the clone is removed by stop()).
    """

    def __init__(self, args, workdir, dfu=False, display=False, scratch_nand=False, extra_args=()):
        self.port = free_port()
        self.workdir = workdir
        self.serial_log = os.path.join(workdir, "serial.log")
        self.nand = args.nand
        self.scratch_nand = None
        if scratch_nand:
            self.scratch_nand = tempfile.mkdtemp(prefix="nand.", dir=workdir)
            os.rmdir(self.scratch_nand)
            subprocess.run(["cp", "-Rc", args.nand, self.scratch_nand], check=True)
            self.nand = self.scratch_nand

        machine = "iPod-Touch,bootrom=%s,nand=%s,nor=%s,usb-chardev=usb" % (args.bootrom, self.nand, args.nor)
        if dfu:
            machine += ",dfu=on"
        cmd = [
            args.qemu, "-M", machine, "-cpu", "max", "-m", "2G", "-monitor", "none",
            "-serial", "file:" + self.serial_log,
            "-chardev", "socket,id=usb,host=127.0.0.1,port=%d,server=on,wait=off,nodelay=on" % self.port,
        ]
        if not display:
            cmd += ["-display", "none"]
        cmd += list(extra_args)
        self.log = open(os.path.join(workdir, "qemu.log"), "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)

    def serial(self):
        try:
            with open(self.serial_log, errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def check_running(self):
        if self.proc.poll() is not None:
            raise EmulatorError("QEMU exited with %d" % self.proc.returncode)

    def connect(self):
        link = None

        def try_connect():
            nonlocal link
            self.check_running()
            try:
                link = USBLink("127.0.0.1", self.port)
                return True
            except ConnectionRefusedError:
                return False

        wait_for(try_connect, 10, "the USB link socket")
        return link

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.log.close()
        if self.scratch_nand:
            shutil.rmtree(self.scratch_nand, ignore_errors=True)
