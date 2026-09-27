"""
Host side of the USB link to the emulated iPod Touch 2G.

QEMU exposes the USB OTG controller on a chardev (see the protocol description
in hw/arm/ipod_touch_usb_otg.c). Every request is an 8-byte header, optionally
followed by a payload, and QEMU answers each request in order with a header of
the same shape. A request is only answered once the guest has armed the
endpoint, so no delays are needed on this side.
"""
import socket
import struct

HEADER = struct.Struct("<BBBBI")

SETUP = 1
OUT = 2
IN = 3
RESET = 4

STATUS_OK = 0
STATUS_STALL = 1


class USBStall(Exception):
    pass


class USBLink:
    def __init__(self, host="127.0.0.1", port=1235):
        self.sock = socket.create_connection((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self):
        self.sock.close()

    def _recv_exact(self, length):
        buf = bytearray()
        while len(buf) < length:
            chunk = self.sock.recv(length - len(buf))
            if not chunk:
                raise ConnectionError("USB link closed by QEMU")
            buf += chunk
        return bytes(buf)

    def _request(self, req_type, ep, length, payload=b""):
        self.sock.sendall(HEADER.pack(req_type, ep, 0, 0, length) + payload)

        rtype, rep, status, _, rlength = HEADER.unpack(self._recv_exact(HEADER.size))
        if rtype != req_type or rep != ep:
            raise ConnectionError("unexpected reply type %d ep 0x%02x" % (rtype, rep))

        data = self._recv_exact(rlength) if req_type == IN else b""
        if status == STATUS_STALL:
            raise USBStall("ep 0x%02x stalled" % ep)
        if status != STATUS_OK:
            raise ConnectionError("request failed with status %d" % status)
        return rlength, data

    def reset(self):
        self._request(RESET, 0, 0)

    def bulk_write(self, ep, data):
        return self._request(OUT, ep, len(data), data)[0]

    def bulk_read(self, ep, length):
        return self._request(IN, ep | 0x80, length)[1]

    def ctrl_transfer(self, bm_request_type, b_request, w_value=0, w_index=0, data_or_length=None):
        """Same calling convention as pyusb's ctrl_transfer."""
        is_in = bm_request_type & 0x80
        if is_in:
            w_length = data_or_length or 0
            data = b""
        else:
            data = bytes(data_or_length or b"")
            w_length = len(data)

        setup = struct.pack("<BBHHH", bm_request_type, b_request, w_value, w_index, w_length)
        self._request(SETUP, 0, len(setup), setup)

        if is_in:
            result = self._request(IN, 0x80, w_length)[1] if w_length else b""
            self._request(OUT, 0, 0)  # status stage
            return result

        if w_length:
            self._request(OUT, 0, w_length, data)
        self._request(IN, 0x80, 0)  # status stage
        return w_length

    def get_descriptor(self, desc_type, index, length, lang_id=0):
        return self.ctrl_transfer(0x80, 0x06, (desc_type << 8) | index, lang_id, length)

    def get_string(self, index):
        desc = self.get_descriptor(0x03, index, 0xFF, 0x0409)
        return desc[2:desc[0]].decode("utf-16-le")
