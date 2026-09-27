"""
Uploads an image to the emulated iPod Touch 2G in DFU mode, the way irecovery
does it for a real device.

Start QEMU with the USB link and DFU mode enabled, e.g.:
  -chardev socket,id=usb,host=127.0.0.1,port=1235,server=on,wait=off,nodelay=on
  -M iPod-Touch,...,usb-chardev=usb,dfu=on
"""
import argparse
import struct
import time

from usb_link import USBLink

DFU_DNLOAD = 1
DFU_GETSTATUS = 3

DFU_BLOCK_SIZE = 0x800

DFU_STATES = {
    0x00: "appIDLE",
    0x01: "appDETACH",
    0x02: "dfuIDLE",
    0x03: "dfuDNLOAD-SYNC",
    0x04: "dfuDNBUSY",
    0x05: "dfuDNLOAD-IDLE",
    0x06: "dfuMANIFEST-SYNC",
    0x07: "dfuMANIFEST",
    0x08: "dfuMANIFEST-WAIT-RESET",
    0x09: "dfuUPLOAD-IDLE",
    0x0A: "dfuERROR",
}


def dfu_get_status(link):
    status = link.ctrl_transfer(0xA1, DFU_GETSTATUS, 0, 0, 6)
    return status[0], status[4]


def print_device_info(link):
    desc = link.wait_for_device()
    vid, pid = struct.unpack_from("<HH", desc, 8)
    print("Device %04x:%04x" % (vid, pid))
    if desc[16]:
        print("Serial: %s" % link.get_string(desc[16]))


def dfu_upload(link, data):
    blocks = [data[i:i + DFU_BLOCK_SIZE] for i in range(0, len(data), DFU_BLOCK_SIZE)]
    for block_num, block in enumerate(blocks):
        link.ctrl_transfer(0x21, DFU_DNLOAD, block_num, 0, block)
        status, state = dfu_get_status(link)
        if status != 0:
            raise RuntimeError("DFU error %d in state %s after block %d"
                               % (status, DFU_STATES.get(state, state), block_num))

    # A zero-length download plus a few status requests start the manifest
    # phase, and a bus reset then makes the bootrom boot the image.
    link.ctrl_transfer(0x21, DFU_DNLOAD, len(blocks), 0, b"")
    for _ in range(3):
        status, state = dfu_get_status(link)
        print("DFU status %d, state %s" % (status, DFU_STATES.get(state, state)))
    link.reset()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="image to upload, e.g. an LLB or iBSS IMG3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1235)
    args = parser.parse_args()

    with open(args.image, "rb") as f:
        data = f.read()

    link = USBLink(args.host, args.port)
    try:
        print_device_info(link)

        start = time.monotonic()
        dfu_upload(link, data)
        elapsed = time.monotonic() - start
        print("Uploaded %d bytes in %.2f s (%.1f KB/s)" % (len(data), elapsed, len(data) / 1024 / elapsed))
    finally:
        link.close()


if __name__ == "__main__":
    main()
