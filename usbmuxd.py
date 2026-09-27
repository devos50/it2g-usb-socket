"""
A usbmuxd replacement that serves the emulated device, so libimobiledevice
tools can talk to iOS running in QEMU:

  python3 usbmuxd.py [--listen 127.0.0.1:27015]
  USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 idevice_id -l
  USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 iproxy 2222 22

It keeps the USB link to QEMU open, reconnecting when QEMU restarts, and
reports the device as attached while iOS is on the bus. Clients use the
plist flavour of the usbmuxd protocol: every message has a 16-byte little
endian header (length, version 1, type 8, tag) followed by an XML plist.
After a successful Connect the client socket carries the raw TCP stream.
Pair records are kept in their own directory (--pair-records), not in
/var/db/lockdown.
"""
import argparse
import os
import plistlib
import socket
import struct
import sys
import threading
import time
import uuid

from usb_link import USBLink, USBError
from usbmux import MuxDevice, MuxError

HEADER = struct.Struct("<IIII")
PLIST_VERSION = 1
PLIST_TYPE = 8

RESULT_OK = 0
RESULT_BADCOMMAND = 1
RESULT_BADDEV = 2
RESULT_CONNREFUSED = 3
RESULT_BADVERSION = 6

DEFAULT_PAIR_RECORDS = os.path.expanduser("~/.it2g-usbmuxd")


def log(msg):
    print("usbmuxd: %s" % msg, file=sys.stderr, flush=True)


class DeviceManager:
    """Owns the link to QEMU and the mux device on it, if any."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.mux = None
        self.device_id = 0
        self.properties = None
        self._listeners = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                link = USBLink(self.host, self.port)
            except (OSError, USBError):
                time.sleep(1)
                continue
            try:
                mux = MuxDevice(link)
                mux.handshake()
            except (USBError, MuxError, ConnectionError, OSError) as e:
                log("no device yet (%s)" % e)
                link.close()
                time.sleep(1)
                continue
            self._attach(mux)
            mux.closed.wait()
            self._detach()
            mux.close()

    def _attach(self, mux):
        with self._lock:
            self.device_id += 1
            self.mux = mux
            self.properties = {
                "ConnectionSpeed": 480000000,
                "ConnectionType": "USB",
                "DeviceID": self.device_id,
                "LocationID": 0,
                "ProductID": mux.pid,
                "SerialNumber": mux.serial,
            }
            listeners = list(self._listeners)
        log("device %d attached: %s" % (self.device_id, mux.serial))
        for client in listeners:
            client.send_attached(self.device_id, self.properties)

    def _detach(self):
        with self._lock:
            device_id, self.mux, self.properties = self.device_id, None, None
            listeners = list(self._listeners)
        log("device %d detached" % device_id)
        for client in listeners:
            client.send_message({"MessageType": "Detached", "DeviceID": device_id})

    def current(self):
        with self._lock:
            return (self.device_id, self.mux, self.properties) if self.mux else (None, None, None)

    def add_listener(self, client):
        with self._lock:
            self._listeners.add(client)
            current = (self.device_id, self.properties) if self.mux else None
        if current:
            client.send_attached(*current)

    def remove_listener(self, client):
        with self._lock:
            self._listeners.discard(client)


class PairRecords:
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        config = os.path.join(path, "SystemConfiguration.plist")
        if not os.path.exists(config):
            with open(config, "wb") as f:
                plistlib.dump({"SystemBUID": str(uuid.uuid4()).upper()}, f)
        with open(config, "rb") as f:
            self.buid = plistlib.load(f)["SystemBUID"]

    def _file(self, record_id):
        if not record_id or "/" in record_id or record_id.startswith("."):
            raise ValueError("bad pair record id %r" % record_id)
        return os.path.join(self.path, record_id + ".plist")

    def read(self, record_id):
        try:
            with open(self._file(record_id), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def save(self, record_id, data):
        with open(self._file(record_id), "wb") as f:
            f.write(data)

    def delete(self, record_id):
        try:
            os.remove(self._file(record_id))
        except FileNotFoundError:
            pass


class Client:
    def __init__(self, sock, devices, records):
        self.sock = sock
        self.devices = devices
        self.records = records
        self._send_lock = threading.Lock()
        self.tag = 0

    def _recv_exact(self, length):
        buf = bytearray()
        while len(buf) < length:
            chunk = self.sock.recv(length - len(buf))
            if not chunk:
                raise ConnectionError("client closed")
            buf += chunk
        return bytes(buf)

    def send_message(self, message, tag=0):
        body = plistlib.dumps(message)
        with self._send_lock:
            self.sock.sendall(HEADER.pack(HEADER.size + len(body), PLIST_VERSION, PLIST_TYPE, tag) + body)

    def send_attached(self, device_id, properties):
        try:
            self.send_message({"MessageType": "Attached", "DeviceID": device_id, "Properties": properties})
        except OSError:
            pass

    def result(self, number):
        self.send_message({"MessageType": "Result", "Number": number}, self.tag)

    def run(self):
        try:
            while True:
                length, version, msg_type, self.tag = HEADER.unpack(self._recv_exact(HEADER.size))
                body = self._recv_exact(length - HEADER.size)
                if version != PLIST_VERSION or msg_type != PLIST_TYPE:
                    log("unsupported message version %d type %d" % (version, msg_type))
                    self.result(RESULT_BADVERSION)
                    continue
                request = plistlib.loads(body)
                if self.handle(request):
                    return  # the socket now belongs to a tunnel
        except (ConnectionError, OSError, plistlib.InvalidFileException):
            pass
        self.devices.remove_listener(self)
        self.sock.close()

    def handle(self, request):
        kind = request.get("MessageType")
        if kind == "ListDevices":
            device_id, _, properties = self.devices.current()
            devices = []
            if device_id:
                devices.append({"DeviceID": device_id, "MessageType": "Attached", "Properties": properties})
            self.send_message({"DeviceList": devices}, self.tag)
        elif kind == "ListListeners":
            self.send_message({"ListenerList": []}, self.tag)
        elif kind == "Listen":
            self.result(RESULT_OK)
            self.devices.add_listener(self)
        elif kind == "Connect":
            return self.connect(request)
        elif kind == "ReadBUID":
            self.send_message({"BUID": self.records.buid}, self.tag)
        elif kind == "ReadPairRecord":
            data = self.records.read(request.get("PairRecordID"))
            if data is None:
                self.result(RESULT_BADDEV)
            else:
                self.send_message({"PairRecordData": data}, self.tag)
        elif kind == "SavePairRecord":
            self.records.save(request.get("PairRecordID"), request.get("PairRecordData", b""))
            self.result(RESULT_OK)
        elif kind == "DeletePairRecord":
            self.records.delete(request.get("PairRecordID"))
            self.result(RESULT_OK)
        else:
            log("unknown request %r" % kind)
            self.result(RESULT_BADCOMMAND)
        return False

    def connect(self, request):
        device_id, mux, _ = self.devices.current()
        if not mux or request.get("DeviceID") != device_id:
            self.result(RESULT_BADDEV)
            return False
        # libusbmuxd sends the port in network byte order.
        port = request.get("PortNumber", 0)
        port = ((port & 0xFF) << 8) | (port >> 8)
        try:
            conn = mux.connect(port)
        except (MuxError, USBError, ConnectionError, OSError) as e:
            log("connect to port %d failed: %s" % (port, e))
            self.result(RESULT_CONNREFUSED)
            return False
        log("tunnel to port %d" % port)
        self.result(RESULT_OK)
        tunnel(self.sock, conn)
        return True


def tunnel(sock, conn):
    """Copies data both ways between a client socket and a mux connection
    until either side closes."""

    def device_to_client():
        try:
            while True:
                data = conn.recv(0x10000)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def client_to_device():
        try:
            while True:
                data = sock.recv(0x10000)
                if not data:
                    break
                conn.send(data)
        except (OSError, MuxError, USBError):
            pass
        conn.close()
        sock.close()

    threading.Thread(target=device_to_client, daemon=True).start()
    threading.Thread(target=client_to_device, daemon=True).start()


def make_server(address):
    if address.startswith("UNIX:"):
        path = address[5:]
        if os.path.exists(path):
            os.remove(path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
    else:
        host, _, port = address.rpartition(":")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host or "127.0.0.1", int(port)))
    server.listen(16)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", default="127.0.0.1:27015",
                        help="host:port or UNIX:<path> to serve on (default 127.0.0.1:27015)")
    parser.add_argument("--host", default="127.0.0.1", help="QEMU USB link host")
    parser.add_argument("--port", type=int, default=1235, help="QEMU USB link port")
    parser.add_argument("--pair-records", default=DEFAULT_PAIR_RECORDS)
    args = parser.parse_args()

    records = PairRecords(args.pair_records)
    devices = DeviceManager(args.host, args.port)
    server = make_server(args.listen)
    log("listening on %s, use USBMUXD_SOCKET_ADDRESS=%s" % (args.listen, args.listen))
    try:
        while True:
            sock, _ = server.accept()
            threading.Thread(target=Client(sock, devices, records).run, daemon=True).start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
