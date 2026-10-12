#!/usr/bin/env python3
"""Apply the one-byte Wine patch to M-EFCS's universal_ble_plugin.dll.

Stock M-EFCS aborts on startup under Wine: its Bluetooth plugin asks WinRT for
Windows.Devices.Radios.Radio (not implemented by Wine) and then fail-fasts.
The DLL exports a single function, the Flutter plugin registrar; turning its
first instruction into `ret` makes registration a no-op, so BLE is never
initialised. The Blackbox connects over USB, so nothing is lost.

    offset 0xBB0:  40 53 48 83 ec 20   ->   c3 53 48 83 ec 20
                   push rbx; sub rsp,0x20   ret

The script refuses to touch a file it does not recognise, keeps the original
as universal_ble_plugin.dll.orig, and is safe to run twice.

    python3 patch_mefcs.py ~/.local/share/wineprefixes/mvave/drive_c/M-EFCS
    python3 patch_mefcs.py --restore <same dir>
"""
import argparse
import hashlib
import os
import shutil
import sys

DLL_NAME = "universal_ble_plugin.dll"
OFFSET = 0xBB0
ORIGINAL = bytes.fromhex("405348 83ec20")
PATCHED = bytes.fromhex("c35348 83ec20")

# M-EFCS Windows build dated 2026-07-13 (universal_ble_plugin.dll, 438784 B).
# Other builds are accepted only if the bytes at OFFSET match, see --force.
KNOWN_ORIGINAL_SHA256 = {
    "689cf6176585941731305d6d6850790e8c55d626c019b781af33ae3a2cb4acbc",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", help="M-EFCS install dir, or the DLL itself")
    ap.add_argument("--restore", action="store_true",
                    help="put the .orig copy back")
    ap.add_argument("--force", action="store_true",
                    help="patch an unknown build if the bytes at 0xBB0 match")
    args = ap.parse_args()

    dll = args.path
    if os.path.isdir(dll):
        dll = os.path.join(dll, DLL_NAME)
    if not os.path.isfile(dll):
        sys.exit(f"{dll}: not found")
    orig = dll + ".orig"

    if args.restore:
        if not os.path.isfile(orig):
            sys.exit(f"{orig}: no backup to restore")
        shutil.copy2(orig, dll)
        print(f"restored {dll} from {os.path.basename(orig)}")
        return

    data = bytearray(open(dll, "rb").read())
    here = bytes(data[OFFSET:OFFSET + len(ORIGINAL)])
    if here == PATCHED:
        print(f"{dll}: already patched, nothing to do")
        return
    if here != ORIGINAL:
        sys.exit(f"{dll}: unexpected bytes at 0x{OFFSET:X} ({here.hex(' ')}); "
                 "this is not a build this patch was made for")
    digest = sha256(data)
    if digest not in KNOWN_ORIGINAL_SHA256 and not args.force:
        sys.exit(f"{dll}: unknown build (sha256 {digest}).\n"
                 "The bytes at the patch site match, so it is probably fine; "
                 "re-run with --force to patch anyway.")

    if not os.path.exists(orig):
        shutil.copy2(dll, orig)
    data[OFFSET] = PATCHED[0]
    with open(dll, "wb") as fh:
        fh.write(data)
    print(f"patched {dll} (original kept as {os.path.basename(orig)})")


if __name__ == "__main__":
    main()
