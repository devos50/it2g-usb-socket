"""
Talks usbmux to iOS running in the emulator, like usbmuxd does for a real
device, and opens TCP connections to services on the device.

  usbmux.py version              do the mux version handshake
  usbmux.py querytype            ask lockdownd (port 62078) for its type
  usbmux.py getvalue [key]       read a lockdownd value, all values without a key

The device (AppleUSBDeviceMux in iOS 2) speaks mux protocol version 1: every
USB transfer carries one packet with an 8-byte big endian header (protocol,
total length). Protocol 0 is the version handshake, protocol 6 carries a TCP
header followed by data. TCP here is a simplified stream without
retransmissions; windows are exchanged shifted right by 8 bits.
"""
import argparse
import plistlib
import struct
import sys
import threading

from usb_link import USBLink, USBError

MUX_INTERFACE_CLASS = (0xFF, 0xFE, 0x02)
MUX_HEADER = struct.Struct(">II")
MUX_VERSION = 0
MUX_TCP = 6
VERSION_PAYLOAD = struct.Struct(">III")

TCP_HEADER = struct.Struct(">HHIIBBHHH")
TH_FIN = 0x01
TH_SYN = 0x02
TH_RST = 0x04
TH_ACK = 0x10

RX_WINDOW = 0x20000
MAX_SEGMENT = 0x4000
LOCKDOWN_PORT = 62078


class MuxError(Exception):
    pass


class MuxConnection:
    def __init__(self, mux, sport, dport):
        self.mux = mux
        self.sport = sport
        self.dport = dport
        self.tx_seq = 0
        self.tx_ack = 0
        self.tx_win = 0
        self.state = "connecting"
        self._rx = bytearray()
        self._cond = threading.Condition()

    def _send_tcp(self, flags, data=b""):
        header = TCP_HEADER.pack(self.sport, self.dport, self.tx_seq, self.tx_ack,
                                 (TCP_HEADER.size // 4) << 4, flags, RX_WINDOW >> 8, 0, 0)
        self.mux._send(MUX_TCP, header + data)

    def _input(self, flags, seq, ack, win, data):
        with self._cond:
            if flags & TH_RST:
                self.state = "refused" if self.state == "connecting" else "closed"
            elif self.state == "connecting" and flags & TH_SYN and flags & TH_ACK:
                self.tx_seq += 1
                self.tx_ack += 1
                self.tx_win = win << 8
                self.state = "connected"
                self._send_tcp(TH_ACK)
            elif self.state == "connected":
                self.tx_win = win << 8
                if data:
                    self.tx_ack += len(data)
                    self._rx += data
                    self._send_tcp(TH_ACK)
                if flags & TH_FIN:
                    self.state = "closed"
            self._cond.notify_all()

    def _wait_state(self, timeout):
        with self._cond:
            if not self._cond.wait_for(lambda: self.state != "connecting", timeout):
                raise MuxError("connection to port %d timed out" % self.dport)
            if self.state != "connected":
                raise MuxError("connection to port %d refused" % self.dport)

    def send(self, data):
        for offset in range(0, len(data), MAX_SEGMENT):
            segment = data[offset:offset + MAX_SEGMENT]
            with self._cond:
                if self.state != "connected":
                    raise MuxError("connection closed")
                self._send_tcp(TH_ACK, segment)
                self.tx_seq += len(segment)

    def recv_exact(self, length, timeout=10):
        with self._cond:
            if not self._cond.wait_for(lambda: len(self._rx) >= length or self.state != "connected", timeout):
                raise MuxError("timed out waiting for %d bytes" % length)
            if len(self._rx) < length:
                raise MuxError("connection closed")
            data = bytes(self._rx[:length])
            del self._rx[:length]
            return data

    def close(self):
        with self._cond:
            if self.state == "connected":
                self._send_tcp(TH_RST)
            self.state = "closed"
        self.mux._forget(self)


class MuxDevice:
    """Selects the configuration with the usbmux interface and runs the mux
    protocol on its bulk endpoints."""

    def __init__(self, link):
        self.link = link
        desc = link.wait_for_device()
        self.vid, self.pid = struct.unpack_from("<HH", desc, 8)
        self.serial = link.get_string(desc[16]) if desc[16] else ""
        self._find_interface(link.get_configurations(desc[17]))
        link.ctrl_transfer(0x00, 0x09, self.config, 0)  # SET_CONFIGURATION

        self._send_lock = threading.Lock()
        self._conns = {}
        self._conns_lock = threading.Lock()
        self._next_port = 1
        self._version = None
        self._version_event = threading.Event()
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _find_interface(self, configs):
        for config in configs:
            for intf in config["interfaces"]:
                if (intf["class"], intf["subclass"], intf["protocol"]) != MUX_INTERFACE_CLASS:
                    continue
                eps = {ep["address"] & 0x80: ep for ep in intf["endpoints"] if ep["type"] == 2}
                if 0x00 in eps and 0x80 in eps:
                    self.config = config["value"]
                    self.out_ep = eps[0x00]["address"]
                    self.out_mps = eps[0x00]["max_packet"]
                    self.in_ep = eps[0x80]["address"] & 0x7F
                    return
        raise MuxError("device has no usbmux interface")

    def _send(self, protocol, payload):
        packet = MUX_HEADER.pack(protocol, MUX_HEADER.size + len(payload)) + payload
        with self._send_lock:
            self.link.bulk_write(self.out_ep, packet)
            # The device needs the transfer to end to see the packet.
            if len(packet) % self.out_mps == 0:
                self.link.bulk_write(self.out_ep, b"")

    def _read_loop(self):
        buf = bytearray()
        try:
            while True:
                buf += self.link.bulk_read(self.in_ep, 0x10000)
                while len(buf) >= MUX_HEADER.size:
                    protocol, length = MUX_HEADER.unpack_from(buf)
                    if length < MUX_HEADER.size or len(buf) < length:
                        break
                    self._dispatch(protocol, bytes(buf[MUX_HEADER.size:length]))
                    del buf[:length]
        except (USBError, ConnectionError, OSError) as e:
            if not self._closing:
                print("usbmux: reader stopped: %s" % e, file=sys.stderr)
            with self._conns_lock:
                conns = list(self._conns.values())
            for conn in conns:
                conn._input(TH_RST, 0, 0, 0, b"")

    def _dispatch(self, protocol, payload):
        if protocol == MUX_VERSION:
            self._version = VERSION_PAYLOAD.unpack_from(payload)[:2]
            self._version_event.set()
        elif protocol == MUX_TCP and len(payload) >= TCP_HEADER.size:
            sport, dport, seq, ack, offset, flags, win, _, _ = TCP_HEADER.unpack_from(payload)
            with self._conns_lock:
                conn = self._conns.get(dport)
            if conn:
                conn._input(flags, seq, ack, win, payload[(offset >> 4) * 4:])
        else:
            print("usbmux: dropping packet with protocol %d" % protocol, file=sys.stderr)

    def handshake(self, timeout=5):
        """Returns the (major, minor) mux version of the device."""
        self._send(MUX_VERSION, VERSION_PAYLOAD.pack(1, 0, 0))
        if not self._version_event.wait(timeout):
            raise MuxError("no version reply")
        return self._version

    def connect(self, port, timeout=10):
        with self._conns_lock:
            sport = self._next_port
            self._next_port = self._next_port % 0xFFFF + 1
            conn = MuxConnection(self, sport, port)
            self._conns[sport] = conn
        conn._send_tcp(TH_SYN)
        try:
            conn._wait_state(timeout)
        except MuxError:
            self._forget(conn)
            raise
        return conn

    def close(self):
        self._closing = True
        self.link.close()
        self._reader.join(timeout=1)

    def _forget(self, conn):
        with self._conns_lock:
            self._conns.pop(conn.sport, None)


def lockdown_request(conn, request):
    body = plistlib.dumps(request)
    conn.send(struct.pack(">I", len(body)) + body)
    length = struct.unpack(">I", conn.recv_exact(4))[0]
    return plistlib.loads(conn.recv_exact(length))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1235)
    parser.add_argument("--pcap", help="record the USB traffic to this file")
    parser.add_argument("command", choices=("version", "querytype", "getvalue"))
    parser.add_argument("key", nargs="?")
    args = parser.parse_args()

    link = USBLink(args.host, args.port, pcap=args.pcap)
    mux = None
    try:
        mux = MuxDevice(link)
        print("Device %04x:%04x %s, configuration %d, endpoints 0x%02x/0x%02x" % (
            mux.vid, mux.pid, mux.serial, mux.config, mux.out_ep, mux.in_ep | 0x80))
        print("Mux version %d.%d" % mux.handshake())
        if args.command == "querytype":
            conn = mux.connect(LOCKDOWN_PORT)
            reply = lockdown_request(conn, {"Label": "usbmux.py", "Request": "QueryType"})
            print(reply.get("Type", reply))
            conn.close()
        elif args.command == "getvalue":
            conn = mux.connect(LOCKDOWN_PORT)
            request = {"Label": "usbmux.py", "Request": "GetValue"}
            if args.key:
                request["Key"] = args.key
            reply = lockdown_request(conn, request)
            print(plistlib.dumps(reply).decode() if "Value" not in reply or isinstance(reply["Value"], dict)
                  else reply["Value"])
            conn.close()
    except (MuxError, USBError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    finally:
        if mux:
            mux.close()
        else:
            link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
