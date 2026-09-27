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

- `usbmux.py`: talks usbmux to a booted iOS, like usbmuxd. It selects the
  configuration with the mux interface (ff/fe/02), does the version
  handshake (iOS 2 speaks mux protocol version 1) and opens TCP connections
  to device ports:
  - `version`: the mux version handshake
  - `querytype`: asks lockdownd (port 62078) for its type
  - `getvalue [key]`: reads lockdownd values (without pairing, only the
    public ones such as `UniqueDeviceID`)
- `usbmuxd.py`: a usbmuxd replacement for libimobiledevice. It keeps the
  USB link open (reconnecting when QEMU restarts) and serves the usbmuxd plist
  protocol on 127.0.0.1:27015 (`--listen host:port` or `UNIX:<path>`). Pair
  records are stored in `~/.it2g-usbmuxd`, not in `/var/db/lockdown`:

  ```
  python3 usbmuxd.py
  export USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015
  idevice_id -l
  ideviceinfo
  iproxy 2222:62078
  ```

  The device appears once iOS has gone on the bus, which needs the
  `com.apple.usbptpd.plist` launch daemon on the root filesystem: the kernel
  only connects when every function of the USB configuration (including PTP)
  has registered.

## Running iOS with libimobiledevice

`run_ios.py` boots iOS from the NAND with the USB link and serves it through
the usbmuxd bridge, in one command (`--headless` for no window,
`--scratch-nand` to boot a throwaway clone so the NAND is not modified):

```
python3 run_ios.py --scratch-nand
export USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015
idevicepair pair
idevicesyslog
afcclient ls /
ideviceinstaller list --system
```

Tested on iOS 2.1.1: pairing, `ideviceinfo`, `idevicesyslog` (needs the
syslogd launch daemon), AFC (`afcclient`, ~20 MB/s up, ~55 MB/s down),
`ideviceinstaller`, `idevicenotificationproxy`, `idevicename`, `idevicedate`
and `iproxy`. `idevicediagnostics` and `idevicecrashreport` use services that
iOS 2 does not have.

## Smoke test

`python3 smoke_test.py` starts its own QEMU (paths default to the usual
locations, see `--help`), walks DFU -> iBSS -> iBEC over USB and checks
enumeration, commands, the serial console, uploads and `go`. It takes a few
seconds and exits non-zero on failure.

`python3 smoke_test.py --ios` instead boots iOS from a clone of the NAND and
checks that it attaches through the usbmuxd bridge and answers lockdownd
(about 15 s when headless).

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
