"""
Boots iOS in the emulator with the USB link and serves it to libimobiledevice
through the usbmuxd bridge, in one command:

  python3 run_ios.py [--headless] [--scratch-nand]
  export USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015
  ideviceinfo

The serial log goes to a temporary directory (printed at start). Ctrl-C stops
QEMU and the bridge.
"""
import argparse
import signal
import sys
import tempfile
import threading

import emulator
from usbmuxd import DEFAULT_PAIR_RECORDS, start_bridge


def stop_on_sigterm(signum, frame):
    # Clean up as on Ctrl-C.
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    emulator.add_arguments(parser)
    parser.add_argument("--headless", action="store_true", help="run without the QEMU window")
    parser.add_argument("--scratch-nand", action="store_true",
                        help="boot a throwaway clone of the NAND so the run does not modify it")
    parser.add_argument("--listen", default="127.0.0.1:27015")
    parser.add_argument("--pair-records", default=DEFAULT_PAIR_RECORDS)
    parser.add_argument("qemu_args", nargs="*", help="extra QEMU arguments, after --")
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="it2g-ios-")
    emu = emulator.Emulator(args, workdir, display=not args.headless,
                            scratch_nand=args.scratch_nand, extra_args=args.qemu_args)
    print("QEMU logs in %s" % workdir, flush=True)
    start_bridge(args.listen, port=emu.port, pair_records=args.pair_records)
    print("export USBMUXD_SOCKET_ADDRESS=%s" % args.listen, flush=True)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    done = threading.Event()
    threading.Thread(target=lambda: (emu.proc.wait(), done.set()), daemon=True).start()
    try:
        done.wait()
        print("QEMU exited with %d" % emu.proc.returncode)
    except KeyboardInterrupt:
        pass
    finally:
        emu.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
