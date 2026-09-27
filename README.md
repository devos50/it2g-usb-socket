# it2g-usb-socket

Host-side tools for the USB link of the iPod Touch 2G QEMU emulator. They play
the role of the USB host (the Mac) and talk to the emulated OTG controller over
a socket.

## Starting QEMU

Add a socket chardev and point the machine at it. `dfu=on` holds the force-DFU
GPIO so the bootrom enters DFU mode:

```
-chardev socket,id=usb,host=127.0.0.1,port=1235,server=on,wait=off,nodelay=on
-M iPod-Touch,...,usb-chardev=usb,dfu=on
```

`-trace 'ipod_touch_usb_*'` logs link requests, endpoint activity and register
accesses.

## Tools

- `usb_link.py`: the link library, with pyusb-style `ctrl_transfer`,
  `bulk_read` and `bulk_write`, timeouts, and optional pcap capture
  (`USBLink(pcap="usb.pcap")`) that Wireshark decodes.
- `main.py <image>`: uploads an image in DFU mode, the way irecovery does.
- `recovery.py`: talks to iBSS/iBEC/iBoot in recovery mode:
  - `info`: device and string descriptors
  - `cmd <command>`: runs an iBoot command (see `cmd help` on the console)
  - `upload <file>`: uploads a file to the load address
  - `shell`: interactive console over the USB serial interface

`recovery.py --pcap <file>` records the traffic for Wireshark.

## Smoke test

`python3 smoke_test.py` starts its own QEMU (paths default to the usual
locations, see `--help`), walks DFU -> iBSS -> iBEC over USB and checks
enumeration, commands, the serial console, uploads and `go`. It takes a few
seconds and exits non-zero on failure.

## Booting iBEC over USB

With the 2.1.1 restore files (iBSS and iBEC are not encrypted):

```
python3 main.py iBSS.n72ap.RELEASE.dfu
python3 recovery.py upload iBEC.n72ap.RELEASE.dfu
python3 recovery.py cmd go
python3 recovery.py shell
```

iBoot stalls a command request when the command is unknown or fails, and it
has no way to return command output over EP0; output appears on the serial
console (`shell`) instead.
