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

## SSH into iOS

The filesystem image built by `generate_nand_it2g` has OpenSSH 5.2, bash and
coreutils (see its README). launchd starts `sshd` when something connects to
port 22, which `iproxy` forwards over usbmux. In separate terminals:

```
python3 run_ios.py
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 iproxy 2222 22
ssh -p 2222 -o HostKeyAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1
```

Start `iproxy` once the device is attached (`idevice_id -l` lists it). The
password is `alpine`, for both `root` and `mobile`. A modern OpenSSH client
only talks to OpenSSH 5.2 with the `ssh-rsa` host key algorithm and the
`diffie-hellman-group14-sha1` key exchange enabled. Without `--scratch-nand`,
changes made over SSH stay on the NAND.

To just type `ssh ipod`, add this to `~/.ssh/config`:

```
Host ipod
    HostName 127.0.0.1
    Port 2222
    User root
    HostKeyAlgorithms +ssh-rsa
    KexAlgorithms +diffie-hellman-group14-sha1
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
```

Files can be copied with `sftp ipod` or `scp -O` (the legacy scp protocol),
e.g. `scp -O ipod:/var/mobile/Library/Preferences/com.apple.springboard.plist .`.

On the device:

- `launchctl stop com.apple.SpringBoard` restarts SpringBoard (a respring):
  its launch daemon has `KeepAlive`, so launchd starts it again.
- There is no `ps`, `killall` or `grep`. `launchctl list` shows the PIDs of
  launchd jobs, for use with `kill`.
- There is no `plutil`. Most plists are binary, so edit them on the Mac
  (`plutil -convert xml1`) and copy them back.

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

## Booting the restore ramdisk

`restore_boot.py` does what iTunes does at the start of a restore: DFU ->
iBSS -> iBEC, then uploads the restore ramdisk (`ramdisk`) and the kernelcache
and runs `bootx`. The ramdisk kernel then starts `restored`, which listens on
the lockdown port over usbmux:

```
python3 restore_boot.py --query
```

`--query` asks restored for its type (`com.apple.mobile.restored`, restore
protocol version 11), `--bridge` serves the device to libimobiledevice, and
`--update` boots the update ramdisk instead. The NAND is a throwaway clone
unless `--persistent-nand` is given. The release iBEC ignores `boot-args` and
boots with `rd=md0 nand-enable-reformat=1 -progress`, so the kernel prints
nothing on the serial port; the display shows the progress bar.
