"""
Makes a restored device (restore_boot.py --restore --output DIR) boot to
SpringBoard in the emulator, by changing the filesystems on its NAND:

- SpringBoard's launch daemon gets LK_ENABLE_MBX2D=0, so LayerKit draws in
  software. The emulator does not model the MBX 2D core, and stock SpringBoard
  waits forever for its blits ("AppleMBXDevice: Graphics Recovery Event").
- With --lockdown, the Lockdown directory of an activated device (activation
  record, data ark, device keys, pair records) goes to
  /private/var/root/Library/Lockdown, so the device is activated. A fresh
  restore is unactivated, and an old activation record cannot be sent to
  lockdownd again: it must contain the activation randomness that lockdownd
  generated for that activation.

  python3 prepare_restored.py DIR [--lockdown DIR|IMAGE]

--lockdown takes the Lockdown directory itself, or a filesystem image that has
it under /private/var/root/Library/Lockdown (mounted read-only).

The restored image can't be changed before the restore instead: asr on the
restore ramdisk checks Apple's signature of the root filesystem image. So this
takes the disk out of DIR/nand with nand_disk.py, changes the partitions with
add_files.py (files owned by root) and writes a new DIR/nand with
generate_nand --disk, all from generate_nand_it2g. The original NAND is kept
as DIR/nand.restored.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

DEFAULT_TOOLS = os.path.expanduser("~/Documents/generate_nand_it2g")
SPRINGBOARD_PLIST = "/System/Library/LaunchDaemons/com.apple.SpringBoard.plist"
LOCKDOWN_IN_ROOTFS = "/private/var/root/Library/Lockdown"
LOCKDOWN_IN_DATA = "/root/Library/Lockdown"  # the data partition is mounted at /private/var
SYSTEM_PARTITION = "partition1.img"
DATA_PARTITION = "partition2.img"


def attach(image, mountpoint, raw=False):
    args = ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mountpoint]
    if raw:
        args += ["-imagekey", "diskimage-class=CRawDiskImage"]
    subprocess.run(args + [image], check=True, stdout=subprocess.DEVNULL)


def detach(mountpoint):
    subprocess.run(["hdiutil", "detach", mountpoint], check=True, stdout=subprocess.DEVNULL)


def add_files(tools, image, staging, paths):
    subprocess.run([sys.executable, os.path.join(tools, "add_files.py"), image, staging] + paths, check=True)


def patch_springboard(tools, partition, work):
    mountpoint = os.path.join(work, "system")
    os.mkdir(mountpoint)
    attach(partition, mountpoint, raw=True)
    try:
        with open(os.path.join(mountpoint, SPRINGBOARD_PLIST.lstrip("/")), "rb") as f:
            plist = plistlib.load(f)
    finally:
        detach(mountpoint)
    plist.setdefault("EnvironmentVariables", {})["LK_ENABLE_MBX2D"] = "0"

    staging = os.path.join(work, "staging-system")
    path = os.path.join(staging, SPRINGBOARD_PLIST.lstrip("/"))
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    add_files(tools, partition, staging, [SPRINGBOARD_PLIST])


def install_lockdown(tools, partition, source, work):
    mountpoint = None
    if os.path.isfile(source):
        mountpoint = os.path.join(work, "lockdown-source")
        os.mkdir(mountpoint)
        attach(source, mountpoint)
        source = os.path.join(mountpoint, LOCKDOWN_IN_ROOTFS.lstrip("/"))
    try:
        staging = os.path.join(work, "staging-data")
        paths = []
        for root, dirs, files in os.walk(source):
            for name in files:
                if name == ".DS_Store":
                    continue
                rel = os.path.relpath(os.path.join(root, name), source)
                path = os.path.join(LOCKDOWN_IN_DATA, rel)
                dst = os.path.join(staging, path.lstrip("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(root, name), dst)
                paths.append(path)
        if not paths:
            raise SystemExit("no files in %s" % source)
    finally:
        if mountpoint:
            detach(mountpoint)
    add_files(tools, partition, staging, paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dir", help="output directory of restore_boot.py --restore --output")
    parser.add_argument("--lockdown", help="Lockdown directory, or an image containing %s" % LOCKDOWN_IN_ROOTFS)
    parser.add_argument("--tools", default=DEFAULT_TOOLS,
                        help="generate_nand_it2g directory (nand_disk.py, add_files.py, generate_nand)")
    args = parser.parse_args()

    nand = os.path.join(args.dir, "nand")
    original = os.path.join(args.dir, "nand.restored")
    if os.path.exists(original):
        parser.error("%s exists: this directory was prepared already" % original)
    nand_disk = os.path.join(args.tools, "nand_disk.py")

    work = tempfile.mkdtemp(prefix="prepare_restored.", dir=args.dir)
    try:
        disk_dir = os.path.join(work, "disk")
        subprocess.run([sys.executable, nand_disk, "extract", nand, disk_dir], check=True)
        patch_springboard(args.tools, os.path.join(disk_dir, SYSTEM_PARTITION), work)
        if args.lockdown:
            install_lockdown(args.tools, os.path.join(disk_dir, DATA_PARTITION), args.lockdown, work)
        new_nand = os.path.join(work, "nand")
        subprocess.run([sys.executable, nand_disk, "build", disk_dir, new_nand], check=True)
        os.rename(nand, original)
        os.rename(new_nand, nand)
        print("%s is ready, the original NAND is in %s" % (nand, original))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
