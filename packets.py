import struct


class TcpUsbHeader:
    def __init__(self, addr, ep, flags, length):
        self.addr = addr
        self.ep = ep
        self.flags = flags
        self.length = length

    def pack(self):
        # Format string: 'BBBh' means three unsigned bytes and one signed short (2 bytes)
        # '<' specifies little-endian byte order, which you might need to adjust based on your requirements
        return struct.pack('<BBBh', self.addr, self.ep, self.flags, self.length)
    
    @staticmethod
    def unpack(data):
        addr, ep, flags, length = struct.unpack('<BBBh', data)
        return TcpUsbHeader(addr, ep, flags, length)
