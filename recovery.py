"""
Talks to the emulated iPod Touch 2G in recovery mode (iBSS / iBEC / iBoot),
like irecovery does for a real device.

  recovery.py info               show the device and its descriptors
  recovery.py cmd <command>      run an iBoot command, e.g. "setenv auto-boot false"
  recovery.py upload <file>      upload a file to the load address
  recovery.py shell              interactive console over the USB serial interface
"""
import argparse
import struct
import sys
import threading
import time

from usb_link import USBLink, USBError, USBNoDevice, USBStall, USBTimeout

RECOVERY_PID = 0x1281
UPLOAD_EP = 0x04
SERIAL_INTERFACE = 1
SERIAL_IN_EP = 0x01
UPLOAD_CHUNK = 0x8000


def connect(args):
    link = USBLink(args.host, args.port, pcap=args.pcap)
    desc = link.wait_for_device()
    vid, pid = struct.unpack_from("<HH", desc, 8)
    if pid != RECOVERY_PID:
        print("warning: device %04x:%04x is not in recovery mode" % (vid, pid), file=sys.stderr)
    link.ctrl_transfer(0x00, 0x09, 1, 0)  # SET_CONFIGURATION
    return link, desc


def send_command(link, command, timeout=5):
    """Runs an iBoot command. iBoot stalls the request when the command is
    unknown or fails. Commands like "go" never finish the request because the
    device leaves recovery mode."""
    try:
        link.ctrl_transfer(0x40, 0, 0, 0, command.encode() + b"\0", timeout=timeout)
        return True
    except USBStall:
        print("command failed: %s" % command, file=sys.stderr)
        return False
    except (USBNoDevice, USBTimeout):
        print("device left recovery mode", file=sys.stderr)
        return True


def upload(link, data):
    link.ctrl_transfer(0x41, 0, 0, 0)
    for offset in range(0, len(data), UPLOAD_CHUNK):
        link.bulk_write(UPLOAD_EP, data[offset:offset + UPLOAD_CHUNK])


def cmd_info(link, desc, args):
    vid, pid = struct.unpack_from("<HH", desc, 8)
    print("Device %04x:%04x" % (vid, pid))
    for name, index in (("Manufacturer", desc[14]), ("Product", desc[15]), ("Serial", desc[16])):
        if index:
            print("%s: %s" % (name, link.get_string(index)))

    ep_types = ("control", "isoc", "bulk", "interrupt")
    for config in link.get_configurations(desc[17]):
        name = link.get_string(config["string"]) if config["string"] else ""
        print("Configuration %d: %s (%d mA)" % (config["value"], name, config["max_power"]))
        for intf in config["interfaces"]:
            name = link.get_string(intf["string"]) if intf["string"] else ""
            print("  Interface %d alt %d: class %02x/%02x/%02x %s" % (
                intf["number"], intf["alt"], intf["class"], intf["subclass"], intf["protocol"], name))
            for ep in intf["endpoints"]:
                print("    Endpoint 0x%02x %s, max packet %d" % (ep["address"], ep_types[ep["type"]], ep["max_packet"]))


def cmd_cmd(link, desc, args):
    return 0 if send_command(link, " ".join(args.command)) else 1


def cmd_upload(link, desc, args):
    with open(args.file, "rb") as f:
        data = f.read()
    start = time.monotonic()
    upload(link, data)
    print("Uploaded %d bytes in %.2f s" % (len(data), time.monotonic() - start))


def cmd_shell(link, desc, args):
    # Alternate setting 1 of the serial interface enables its endpoints.
    link.ctrl_transfer(0x01, 0x0B, 1, SERIAL_INTERFACE)

    def read_console():
        while True:
            try:
                data = link.bulk_read(SERIAL_IN_EP, 0x200)
            except USBError:
                print("\n[device disconnected]")
                return
            except ConnectionError:
                return
            sys.stdout.write(data.decode(errors="replace"))
            sys.stdout.flush()

    threading.Thread(target=read_console, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if line in ("exit", "quit"):
            break
        if line:
            send_command(link, line)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1235)
    parser.add_argument("--pcap", help="write the USB traffic to this file for Wireshark")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("info").set_defaults(fn=cmd_info)
    p = sub.add_parser("cmd")
    p.add_argument("command", nargs="+")
    p.set_defaults(fn=cmd_cmd)
    p = sub.add_parser("upload")
    p.add_argument("file")
    p.set_defaults(fn=cmd_upload)
    sub.add_parser("shell").set_defaults(fn=cmd_shell)
    args = parser.parse_args()

    link, desc = connect(args)
    try:
        return args.fn(link, desc, args)
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
