"""
Host side of the USB link to the emulated iPod Touch 2G.

QEMU exposes the USB OTG controller on a chardev (see the protocol description
in hw/arm/ipod_touch_usb_link.c). Every request is a 12-byte header, optionally
followed by a payload, and QEMU answers every request with a header of the
same shape that carries the tag of the request. Requests on different
endpoints are independent, so an IN transfer can stay pending on one endpoint
while other endpoints are used.
"""
import socket
import struct
import threading
import time

HEADER = struct.Struct("<BBBBII")
VERSION = 2

HELLO = 0
SETUP = 1
OUT = 2
IN = 3
RESET = 4
CANCEL = 5

STATUS_OK = 0
STATUS_STALL = 1
STATUS_CANCELLED = 2
STATUS_NODEV = 3


class USBError(Exception):
    pass


class USBStall(USBError):
    pass


class USBTimeout(USBError):
    pass


class USBNoDevice(USBError):
    """The device disconnected, e.g. because it jumped to another image."""


class Transfer:
    def __init__(self, req_type, ep, length):
        self.req_type = req_type
        self.ep = ep
        self.length = length
        self.status = None
        self.data = b""
        self.actual = 0
        self._done = threading.Event()

    def _finish(self, status, actual, data):
        self.status = status
        self.actual = actual
        self.data = data
        self._done.set()

    def done(self):
        return self._done.is_set()

    def wait_done(self, timeout=None):
        """Waits until the transfer is answered, returns False on timeout."""
        return self._done.wait(timeout)


class PcapWriter:
    """Writes transfers in the Linux usbmon format, which Wireshark decodes."""

    LINKTYPE_USB_LINUX_MMAPPED = 220
    RECORD = struct.Struct("<QBBBBHbbqiiII8siiII")
    STATUS_ERRNO = {STATUS_OK: 0, STATUS_STALL: -32, STATUS_CANCELLED: -2, STATUS_NODEV: -19}

    def __init__(self, path):
        self.file = open(path, "wb")
        self.file.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, self.LINKTYPE_USB_LINUX_MMAPPED))
        self.lock = threading.Lock()
        self.next_id = 1

    def new_id(self):
        with self.lock:
            self.next_id += 1
            return self.next_id

    def record(self, urb_id, event, ep, setup, data, status, length):
        xfer_type = 2 if ep & 0x7F == 0 else 3
        now = time.time()
        header = self.RECORD.pack(
            urb_id, ord(event), xfer_type, ep, 1, 1,
            0 if setup else ord("-"), 0 if data else ord("<"),
            int(now), int((now % 1) * 1e6), status, length, len(data),
            setup or bytes(8), 0, 0, 0, 0)
        packet = header + data
        with self.lock:
            self.file.write(struct.pack("<IIII", int(now), int((now % 1) * 1e6), len(packet), len(packet)))
            self.file.write(packet)
            self.file.flush()

    def submit(self, ep, setup, data, length):
        urb_id = self.new_id()
        self.record(urb_id, "S", ep, setup, data, -115, length)
        return urb_id

    def complete(self, urb_id, ep, status, data, length):
        self.record(urb_id, "C", ep, None, data, self.STATUS_ERRNO.get(status, -5), length)

    def close(self):
        self.file.close()


def parse_configuration(data):
    """Splits a full configuration descriptor into a dict with the
    configuration fields and a list of interfaces, each with its endpoints."""
    config = {"value": data[5], "string": data[6], "attributes": data[7], "max_power": data[8] * 2, "interfaces": []}
    offset = data[0]
    while offset + 2 <= len(data) and data[offset] >= 2:
        length, desc_type = data[offset], data[offset + 1]
        d = data[offset:offset + length]
        if desc_type == 0x04 and length >= 9:
            config["interfaces"].append({
                "number": d[2], "alt": d[3], "class": d[5], "subclass": d[6], "protocol": d[7],
                "string": d[8], "endpoints": []})
        elif desc_type == 0x05 and length >= 7 and config["interfaces"]:
            config["interfaces"][-1]["endpoints"].append({
                "address": d[2], "type": d[3] & 3, "max_packet": struct.unpack_from("<H", d, 4)[0] & 0x7ff})
        offset += length
    return config


class USBLink:
    def __init__(self, host="127.0.0.1", port=1235, pcap=None):
        self.sock = socket.create_connection((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.pcap = PcapWriter(pcap) if pcap else None
        self._send_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._next_tag = 1
        self._closed = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        version = self._wait(self._submit(HELLO, 0, 0), 5)
        if version != VERSION:
            raise USBError("QEMU speaks USB link protocol version %d, expected %d" % (version, VERSION))

    def close(self):
        self.sock.close()
        if self.pcap:
            self.pcap.close()

    def _recv_exact(self, length):
        buf = bytearray()
        while len(buf) < length:
            chunk = self.sock.recv(length - len(buf))
            if not chunk:
                raise ConnectionError("USB link closed by QEMU")
            buf += chunk
        return bytes(buf)

    def _read_loop(self):
        try:
            while True:
                rtype, _, status, _, tag, length = HEADER.unpack(self._recv_exact(HEADER.size))
                data = self._recv_exact(length) if rtype == IN else b""
                if rtype == CANCEL:
                    continue  # the cancelled request gets its own answer
                with self._pending_lock:
                    transfer = self._pending.pop(tag, None)
                if transfer:
                    transfer._finish(status, length, data)
        except (ConnectionError, OSError) as e:
            self._closed = e
            with self._pending_lock:
                pending, self._pending = self._pending, {}
            for transfer in pending.values():
                transfer._finish(None, 0, b"")

    def _submit(self, req_type, ep, length, payload=b""):
        transfer = Transfer(req_type, ep, length)
        with self._pending_lock:
            tag = self._next_tag
            self._next_tag = (self._next_tag + 1) & 0xFFFFFFFF or 1
            transfer.tag = tag
            self._pending[tag] = transfer
        with self._send_lock:
            self.sock.sendall(HEADER.pack(req_type, ep, 0, 0, tag, length) + payload)
        return transfer

    def _cancel(self, transfer):
        with self._send_lock:
            self.sock.sendall(HEADER.pack(CANCEL, 0, 0, 0, transfer.tag, 0))

    def _wait(self, transfer, timeout):
        if not transfer._done.wait(timeout):
            if transfer.req_type in (SETUP, OUT, IN):
                self._cancel(transfer)
                transfer._done.wait()
            if transfer.status != STATUS_OK:
                raise USBTimeout("ep 0x%02x timed out" % transfer.ep)

        if transfer.status is None:
            raise ConnectionError("USB link closed: %s" % self._closed)
        if transfer.status == STATUS_STALL:
            raise USBStall("ep 0x%02x stalled" % transfer.ep)
        if transfer.status == STATUS_CANCELLED:
            raise USBTimeout("ep 0x%02x cancelled" % transfer.ep)
        if transfer.status == STATUS_NODEV:
            raise USBNoDevice("device disconnected")
        if transfer.req_type == IN:
            return transfer.data
        return transfer.actual

    def reset(self, timeout=5):
        return self._wait(self._submit(RESET, 0, 0), timeout)

    def submit_bulk_read(self, ep, length):
        """Starts an IN transfer and returns it without waiting. Pass the
        result to wait()."""
        return self._submit(IN, ep | 0x80, length)

    def wait(self, transfer, timeout=None):
        return self._wait(transfer, timeout)

    def _logged(self, ep, setup, out_data, length, fn):
        urb_id = self.pcap.submit(ep, setup, out_data, length) if self.pcap else None
        status, in_data = STATUS_OK, b""
        try:
            result = fn()
            if isinstance(result, bytes):
                in_data = result
            return result
        except USBStall:
            status = STATUS_STALL
            raise
        except USBTimeout:
            status = STATUS_CANCELLED
            raise
        except (USBNoDevice, ConnectionError):
            status = STATUS_NODEV
            raise
        finally:
            if self.pcap:
                self.pcap.complete(urb_id, ep, status, in_data, len(in_data) if ep & 0x80 else length)

    def bulk_write(self, ep, data, timeout=None):
        return self._logged(ep, None, data, len(data),
                            lambda: self._wait(self._submit(OUT, ep, len(data), data), timeout))

    def bulk_read(self, ep, length, timeout=None):
        return self._logged(ep | 0x80, None, b"", length,
                            lambda: self._wait(self.submit_bulk_read(ep, length), timeout))

    def ctrl_transfer(self, bm_request_type, b_request, w_value=0, w_index=0, data_or_length=None, timeout=None):
        """Same calling convention as pyusb's ctrl_transfer."""
        is_in = bm_request_type & 0x80
        if is_in:
            w_length = data_or_length or 0
            data = b""
        else:
            data = bytes(data_or_length or b"")
            w_length = len(data)

        setup = struct.pack("<BBHHH", bm_request_type, b_request, w_value, w_index, w_length)

        def run():
            self._wait(self._submit(SETUP, 0, len(setup), setup), timeout)
            if is_in:
                result = self._wait(self._submit(IN, 0x80, w_length), timeout) if w_length else b""
                self._wait(self._submit(OUT, 0, 0), timeout)  # status stage
                return result
            if w_length:
                self._wait(self._submit(OUT, 0, w_length, data), timeout)
            self._wait(self._submit(IN, 0x80, 0), timeout)  # status stage
            return w_length

        return self._logged(0x80 if is_in else 0x00, setup, data, w_length, run)

    def get_descriptor(self, desc_type, index, length, lang_id=0, timeout=None):
        return self.ctrl_transfer(0x80, 0x06, (desc_type << 8) | index, lang_id, length, timeout)

    def get_string(self, index, timeout=None):
        desc = self.get_descriptor(0x03, index, 0xFF, 0x0409, timeout)
        return desc[2:desc[0]].decode("utf-16-le")

    def get_configurations(self, num_configs, timeout=None):
        """Reads configuration descriptors 0..num_configs-1 and returns them
        as parsed by parse_configuration."""
        configs = []
        for index in range(num_configs):
            header = self.get_descriptor(0x02, index, 9, timeout=timeout)
            total = struct.unpack_from("<H", header, 2)[0]
            configs.append(parse_configuration(self.get_descriptor(0x02, index, total, timeout=timeout)))
        return configs

    def wait_for_device(self, timeout=30):
        """Resets the bus until the device answers, like a host does when a
        device is plugged in. Returns the device descriptor."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.reset(timeout=2)
                return self.get_descriptor(0x01, 0, 18, timeout=1)
            except (USBTimeout, USBNoDevice, USBStall):
                if time.monotonic() > deadline:
                    raise USBTimeout("no device appeared within %d s" % timeout)
                time.sleep(0.2)
