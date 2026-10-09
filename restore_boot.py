"""
Boots the restore ramdisk of an IPSW in the emulator, the way iTunes starts a
restore: DFU -> iBSS -> iBEC, then upload the ramdisk and kernelcache over USB
and `bootx`.

  python3 restore_boot.py [--ipsw DIR] [--update] [--display] [--query] [--restore] [--bridge]

The device then runs `restored`, which listens on the lockdown port over
usbmux. --query sends it QueryType, --restore runs the restore
with restore.py, and with --bridge the usbmuxd bridge serves
it on 127.0.0.1:27015 (USBMUXD_SOCKET_ADDRESS). The NAND is a throwaway clone
unless --persistent-nand is given. Ctrl-C stops QEMU.

The release iBEC ignores the boot-args variable and boots the ramdisk with
"rd=md0 nand-enable-reformat=1 -progress", so the kernel prints nothing on the
serial port.
"""
import argparse
import os
import plistlib
import signal
import struct
import sys
import tempfile
import threading
import time

import emulator
from emulator import EmulatorError, wait_for
from main import dfu_upload
from recovery import upload
from smoke_test import DFU_PID, RECOVERY_PID, command, enumerate_device
from usb_link import USBError, USBNoDevice, USBTimeout

DEFAULT_IPSW = os.path.expanduser("~/Downloads/iPod2,1_2.1.1_5F138_Restore")


def restore_images(ipsw, update):
    """Returns (ramdisk, kernelcache) paths from Restore.plist."""
    with open(os.path.join(ipsw, "Restore.plist"), "rb") as f:
        plist = plistlib.load(f)
    ramdisks = plist["RestoreRamDisks"]
    ramdisk = ramdisks["Update" if update else "User"]
    kernel = plist["RestoreKernelCaches"]["Release"]
    return os.path.join(ipsw, ramdisk), os.path.join(ipsw, kernel)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def step(name, fn):
    start = time.monotonic()
    fn()
    print("ok    %-36s %.2f s" % (name, time.monotonic() - start), flush=True)


def boot_ramdisk(emu, link, args):
    dfu = os.path.join(args.ipsw, "Firmware", "dfu")
    ibss = read(os.path.join(dfu, "iBSS.n72ap.RELEASE.dfu"))
    ibec = read(os.path.join(dfu, "iBEC.n72ap.RELEASE.dfu"))
    ramdisk_path, kernel_path = restore_images(args.ipsw, args.update)
    ramdisk = read(ramdisk_path)
    kernel = read(kernel_path)

    def to_recovery(image, marker):
        enumerate_device(link, DFU_PID)
        dfu_upload(link, image)
        wait_for(lambda: marker in emu.serial(), 20, "iBSS to enter recovery mode")

    def ibss_loads_ibec():
        enumerate_device(link, RECOVERY_PID)
        link.ctrl_transfer(0x00, 0x09, 1, 0)
        upload(link, ibec)
        try:
            command(link, "go")
        except (USBNoDevice, USBTimeout):
            pass
        wait_for(lambda: "iBEC for n72ap" in emu.serial(), 20, "iBEC to start")

    def ibec_ready():
        enumerate_device(link, RECOVERY_PID)
        link.ctrl_transfer(0x00, 0x09, 1, 0)

    def load_ramdisk():
        upload(link, ramdisk)
        command(link, "ramdisk", timeout=30)

    def boot():
        try:
            command(link, "bootx", timeout=30)
        except (USBNoDevice, USBTimeout):
            pass

    step("DFU upload of iBSS", lambda: to_recovery(ibss, "Entering recovery mode"))
    step("iBSS loads iBEC", ibss_loads_ibec)
    step("iBEC enumerates", ibec_ready)
    step("upload %s + ramdisk" % os.path.basename(ramdisk_path), load_ramdisk)
    step("upload %s" % os.path.basename(kernel_path), lambda: upload(link, kernel))
    step("bootx", boot)


def query_restored(emu, timeout):
    """Waits for the restore ramdisk to come up on USB and asks the service
    on the lockdown port what it is (com.apple.mobile.restored)."""
    from usbmux import LOCKDOWN_PORT, MuxDevice, lockdown_request
    link = emu.connect()
    mux = None
    try:
        start = time.monotonic()
        desc = link.wait_for_device(timeout=timeout)
        print("ok    device on USB, %04x:%04x               %.2f s"
              % (*struct.unpack_from("<HH", desc, 8), time.monotonic() - start), flush=True)
        mux = MuxDevice(link)
        print("      mux version %d.%d, serial %s" % (*mux.handshake(), mux.serial), flush=True)
        conn = mux.connect(LOCKDOWN_PORT, timeout=timeout)
        reply = lockdown_request(conn, {"Label": "restore_boot.py", "Request": "QueryType"})
        print("ok    QueryType: %s" % reply, flush=True)
        conn.close()
    finally:
        if mux:
            mux.close()
        else:
            link.close()


def restore(emu, timeout):
    """Waits for restored and runs the restore with restore.py."""
    from restore import RestoreError, probe_asr, run_restore
    from usbmux import MuxError
    link = emu.connect()
    start = time.monotonic()
    desc = link.wait_for_device(timeout=timeout)
    print("ok    device on USB, %04x:%04x               %.2f s"
          % (*struct.unpack_from("<HH", desc, 8), time.monotonic() - start), flush=True)
    try:
        status = run_restore(link, {"SystemImageData": probe_asr},
                             log=lambda text: print(text, flush=True))
    except (RestoreError, MuxError) as e:
        raise EmulatorError("restore: %s" % e)
    if status != 0:
        raise EmulatorError("restore failed with status %s" % status)


def stop_on_sigterm(signum, frame):
    # Clean up as on Ctrl-C.
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    emulator.add_arguments(parser)
    parser.add_argument("--ipsw", default=DEFAULT_IPSW, help="extracted IPSW directory")
    parser.add_argument("--update", action="store_true", help="boot the update ramdisk instead of the restore one")
    parser.add_argument("--display", action="store_true", help="show the QEMU window")
    parser.add_argument("--persistent-nand", action="store_true",
                        help="let the restore write the real NAND instead of a throwaway clone")
    parser.add_argument("--bridge", action="store_true", help="serve the device through the usbmuxd bridge")
    parser.add_argument("--listen", default="127.0.0.1:27015")
    parser.add_argument("--query", action="store_true", help="wait for restored and send it QueryType")
    parser.add_argument("--restore", action="store_true", help="run the restore (restore.py) once restored is up")
    parser.add_argument("--wait", type=float, default=0,
                        help="stop after this many seconds (0: run until Ctrl-C or QEMU exits)")
    parser.add_argument("qemu_args", nargs="*", help="extra QEMU arguments, after --")
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="it2g-restore-")
    emu = emulator.Emulator(args, workdir, dfu=True, display=args.display,
                            scratch_nand=not args.persistent_nand, extra_args=args.qemu_args)
    print("QEMU logs in %s" % workdir, flush=True)
    signal.signal(signal.SIGTERM, stop_on_sigterm)

    try:
        link = emu.connect()
        try:
            boot_ramdisk(emu, link, args)
        finally:
            link.close()
        if args.query:
            query_restored(emu, timeout=120)
        if args.restore:
            restore(emu, timeout=120)
        if args.bridge:
            from usbmuxd import DEFAULT_PAIR_RECORDS, start_bridge
            start_bridge(args.listen, port=emu.port, pair_records=DEFAULT_PAIR_RECORDS)
            print("export USBMUXD_SOCKET_ADDRESS=%s" % args.listen, flush=True)
        done = threading.Event()
        threading.Thread(target=lambda: (emu.proc.wait(), done.set()), daemon=True).start()
        done.wait(args.wait or None)
        if done.is_set():
            print("QEMU exited with %d" % emu.proc.returncode)
    except (EmulatorError, USBError, KeyboardInterrupt) as e:
        if not isinstance(e, KeyboardInterrupt):
            print("FAIL  %s" % e)
            return 1
    finally:
        emu.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
