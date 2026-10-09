"""
Drives a restore with the restore ramdisk's `restored` (iOS 2.1.1), the way
iTunes does after the ramdisk has booted. Usually run through
`restore_boot.py --restore`.

  python3 restore.py [--port 1235] [--ipsw DIR] [--nor-images DIR]

It serves the root filesystem (SystemRestoreImages in Restore.plist) to asr
and the NOR images (in all_flash manifest order) to restored.

restored listens on the lockdown port over usbmux and speaks length-prefixed
XML plists like lockdownd. StartRestore starts the whole restore; restored
reads its options from /usr/local/share/restore/options.plist on the ramdisk
(absent in 5F138, so every step runs with its default) and ignores any sent
by the host. It then reports each step as a ProgressMsg, asks the host for
data with DataRequestMsg, and finishes with a StatusMsg (Status 0 = success,
otherwise with its log) before it reboots.

Steps in restored_external (progress operation in brackets):
  wait_for_storage_device     [28] until /dev/disk0 exists
  create_filesystem_partitions [11] partition map, [12] wipe + newfs per partition
  restore_images              DataRequestMsg SystemImageData, then
                              `asr -source asr://localhost:12345 -target /dev/disk0s1 -erase`
                              (the host connects to device port 12345 and serves the image)
  mount_filesystems           [15] fsck_hfs, [16] mount_hfs, per partition
  fixup_var                   [17] move /mnt1/private/var to the data partition
  unmount_filesystems         [29]
  clear_persistent_boot_args  [25]
  update_NOR                  DataRequestMsg NORData; the host replies with
                              {LlbImageData: data, NorImageData: [data, ...]}, [18]
  update_baseband             [19] (skipped: no baseband)
  set_boot_stage              [20]
  end_nand_epoch_update       [32]
  set_auto_boot_delay
"""
import argparse
import os
import plistlib
import struct
import sys
import time

from usb_link import USBLink, USBError
from usbmux import LOCKDOWN_PORT, MuxDevice, MuxError

ASR_PORT = 12345
RESTORE_PROTOCOL_VERSION = 11

OPERATIONS = {
    11: "create partition map",
    12: "create filesystem",
    13: "restore image",
    15: "check filesystems",
    16: "mount filesystems",
    17: "fix up /var",
    18: "flash NOR",
    19: "update baseband",
    20: "set boot stage",
    25: "clear boot-args",
    28: "wait for storage device",
    29: "unmount filesystems",
    32: "finalize NAND epoch update",
}


class RestoreError(Exception):
    pass


class PlistConnection:
    """A device connection carrying 32-bit big endian length-prefixed XML plists."""

    def __init__(self, conn):
        self.conn = conn

    def send(self, message):
        body = plistlib.dumps(message)
        self.conn.send(struct.pack(">I", len(body)) + body)

    def recv(self, timeout=None):
        length = struct.unpack(">I", self.conn.recv_exact(4, timeout=timeout))[0]
        return plistlib.loads(self.conn.recv_exact(length, timeout=timeout))

    def close(self):
        self.conn.close()


class ASRConnection:
    """asr on the device sends and expects bare XML plists, without a length."""

    def __init__(self, conn):
        self.conn = conn
        self._buf = b""

    def recv(self, timeout=60):
        while True:
            end = self._buf.find(b"</plist>")
            if end >= 0:
                end += len(b"</plist>")
                body, self._buf = self._buf[:end], self._buf[end:].lstrip(b"\r\n\0")
                return plistlib.loads(body)
            data = self.conn.recv(0x10000, timeout=timeout)
            if not data:
                raise RestoreError("asr closed the connection")
            self._buf += data

    def send(self, message):
        self.conn.send(plistlib.dumps(message))

    def close(self):
        self.conn.close()


class RestoreClient:
    """Runs StartRestore and handles the messages restored sends until it
    reports its final status.

    data_handlers maps a DataRequestMsg DataType to a function(client, message)
    that serves it. Without a handler for a type the restore stops there."""

    def __init__(self, mux, log=print, data_handlers=None):
        self.mux = mux
        self.log = log
        self.data_handlers = dict(data_handlers or {})
        self.conn = PlistConnection(mux.connect(LOCKDOWN_PORT, timeout=30))
        self.start = time.monotonic()
        self.last_operation = None

    def request(self, request, **fields):
        self.conn.send({"Request": request, **fields})
        return self.conn.recv(timeout=30)

    def query(self):
        reply = self.request("QueryType")
        if reply.get("Type") != "com.apple.mobile.restored":
            raise RestoreError("not restored: %s" % reply)
        info = {"RestoreProtocolVersion": reply.get("RestoreProtocolVersion")}
        for key in ("SerialNumber", "HardwareInfo"):
            info[key] = self.request("QueryValue", QueryKey=key).get(key)
        return info

    def _elapsed(self):
        return "%7.1f s" % (time.monotonic() - self.start)

    def run(self, message_timeout=600):
        """Starts the restore and returns restored's final status (0 = success)."""
        self.start = time.monotonic()
        self.conn.send({"Request": "StartRestore", "RestoreProtocolVersion": RESTORE_PROTOCOL_VERSION})
        while True:
            message = self.conn.recv(timeout=message_timeout)
            kind = message.get("MsgType")
            if kind == "ProgressMsg":
                self._progress(message)
            elif kind == "DataRequestMsg":
                data_type = message.get("DataType")
                self.log("%s  data request: %s" % (self._elapsed(), data_type))
                handler = self.data_handlers.get(data_type)
                if handler is None:
                    raise RestoreError("no handler for data request %s" % data_type)
                handler(self, message)
            elif kind == "StatusMsg":
                status = message.get("Status")
                self.log("%s  status: %s" % (self._elapsed(), status))
                for key, value in message.items():
                    if key not in ("MsgType", "Status"):
                        self.log("%s:\n%s" % (key, value))
                return status
            else:
                self.log("%s  unexpected message: %s" % (self._elapsed(), message))

    def _progress(self, message):
        operation = message.get("Operation")
        progress = message.get("Progress", -1)
        name = OPERATIONS.get(operation, "operation %s" % operation)
        if operation != self.last_operation or progress not in (-1, 0xFFFFFFFF):
            suffix = "" if progress in (-1, 0xFFFFFFFF) else " %d%%" % progress
            self.log("%s  %s%s" % (self._elapsed(), name, suffix))
        self.last_operation = operation

    def close(self):
        self.conn.close()


def connect_asr(client, timeout=60):
    """restored starts asr only after sending the data request, so the port
    refuses connections for a moment."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return ASRConnection(client.mux.connect(ASR_PORT, timeout=timeout))
        except MuxError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


class ASRServer:
    """Serves a disk image to asr on the device, like iTunes. asr first asks
    for pieces of the image out of band (the image's metadata), then for the
    whole image as one stream that it writes to the target partition."""

    PAYLOAD_CHUNK = 0x20000

    def __init__(self, path, log=print):
        self.path = path
        self.log = log

    def __call__(self, client, message, timeout=60):
        conn = connect_asr(client, timeout)
        try:
            self.serve(conn)
        finally:
            conn.close()

    def serve(self, conn):
        size = os.path.getsize(self.path)
        initiate = conn.recv()
        if initiate.get("Command") != "Initiate":
            raise RestoreError("asr: expected Initiate, got %s" % initiate)
        self.log("asr: initiate %s" % {k: v for k, v in initiate.items() if k != "Command"})
        conn.send({
            "FEC Slice Stride": 40,
            "Packet Payload Size": 1450,
            "Packets Per FEC": 25,
            "Payload": {"Port": 1, "Size": size},
            "Stream ID": 1,
            "Version": 1,
        })
        with open(self.path, "rb") as f:
            while True:
                request = conn.recv(timeout=300)
                command = request.get("Command")
                if command == "OOBData":
                    offset, length = request["OOB Offset"], request["OOB Length"]
                    f.seek(offset)
                    conn.conn.send(f.read(length))
                elif command == "Payload":
                    break
                else:
                    raise RestoreError("asr: unexpected request %s" % request)
            self.log("asr: sending the %d MB image" % (size >> 20))
            f.seek(0)
            start = time.monotonic()
            sent = 0
            next_report = 0
            while True:
                chunk = f.read(self.PAYLOAD_CHUNK)
                if not chunk:
                    break
                conn.conn.send(chunk)
                sent += len(chunk)
                if sent * 10 // size >= next_report:
                    self.log("asr: %3d%%  %.1f MB/s" % (sent * 100 // size,
                                                      sent / 1e6 / max(time.monotonic() - start, 1e-3)))
                    next_report = sent * 10 // size + 1


def nor_image_paths(ipsw, fallback_dir=None):
    """Returns (LLB, [other images]) in the order of the all_flash manifest.
    Images missing from the IPSW are taken from fallback_dir under the names
    generate_nor uses (llb.img3, iboot.img3, dtree.img3, applelogo.img3...)."""
    flash = os.path.join(ipsw, "Firmware", "all_flash", "all_flash.n72ap.production")
    with open(os.path.join(flash, "manifest")) as f:
        names = [line.strip() for line in f if line.strip()]
    paths = []
    for name in names:
        path = os.path.join(flash, name)
        if not os.path.exists(path) and fallback_dir:
            short = name.split(".")[0].lower().replace("devicetree", "dtree")
            path = os.path.join(fallback_dir, short + ".img3")
        if not os.path.exists(path):
            raise RestoreError("NOR image %s not found" % name)
        paths.append(path)
    return paths[0], paths[1:]


class NORServer:
    """Answers the NORData request with LLB and the other NOR images, which
    restored flashes through AppleImage3NORAccess."""

    def __init__(self, llb, images, log=print):
        self.llb = llb
        self.images = images
        self.log = log

    def __call__(self, client, message):
        def read(path):
            with open(path, "rb") as f:
                return f.read()
        self.log("nor: %s" % ", ".join(os.path.basename(p) for p in [self.llb] + self.images))
        client.conn.send({"LlbImageData": read(self.llb), "NorImageData": [read(p) for p in self.images]})


def run_restore(link, data_handlers=None, log=print):
    mux = MuxDevice(link)
    try:
        log("mux version %d.%d, serial %s" % (*mux.handshake(), mux.serial))
        client = RestoreClient(mux, log=log, data_handlers=data_handlers)
        try:
            info = client.query()
            log("restored: protocol %s, serial %s" % (info["RestoreProtocolVersion"], info["SerialNumber"]))
            if info["HardwareInfo"]:
                log("hardware: %s" % ", ".join("%s=%s" % kv for kv in sorted(info["HardwareInfo"].items())))
            return client.run()
        finally:
            client.close()
    finally:
        mux.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1235)
    parser.add_argument("--ipsw", default=os.path.expanduser("~/Downloads/iPod2,1_2.1.1_5F138_Restore"),
                        help="extracted IPSW directory")
    parser.add_argument("--nor-images", default=os.path.expanduser("~/Documents/generate_nor_it2g/data"),
                        help="NOR img3 files to use when the IPSW lacks them (generate_nor names)")
    args = parser.parse_args()
    try:
        with open(os.path.join(args.ipsw, "Restore.plist"), "rb") as f:
            system_image = os.path.join(args.ipsw, plistlib.load(f)["SystemRestoreImages"]["User"])
        handlers = {
            "SystemImageData": ASRServer(system_image),
            "NORData": NORServer(*nor_image_paths(args.ipsw, args.nor_images)),
        }
        status = run_restore(USBLink(args.host, args.port), handlers)
    except (RestoreError, MuxError, USBError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0 if status == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
