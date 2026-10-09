"""
Drives a restore with the restore ramdisk's `restored` (iOS 2.1.1), the way
iTunes does after the ramdisk has booted. Usually run through
`restore_boot.py --restore`.

  python3 restore.py [--port 1235]

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


def probe_asr(client, message, timeout=60):
    """SystemImageData handler that only connects to asr and reports what it
    asks for, then stops the restore."""
    conn = connect_asr(client, timeout)
    try:
        first = conn.recv(timeout=timeout)
        client.log("asr: %s" % first)
    finally:
        conn.close()
    raise RestoreError("stopping at the system image (no image handler)")


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
    args = parser.parse_args()
    try:
        status = run_restore(USBLink(args.host, args.port), {"SystemImageData": probe_asr})
    except (RestoreError, MuxError, USBError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0 if status == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
