#!/usr/bin/env python3
"""Decode what M-EFCS sends to the pedal, live.

Subscribes to the Wine MIDI driver's output port and prints every frame that
isn't the once-a-second keepalive, so operating the editor by hand becomes a
labelled protocol capture.

    python3 watch_app.py            # run this, then click things in M-EFCS
"""
import os
import re
import time
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blackbox import unpack7, MAGIC, MARK_READ, MARK_WRITE

KEEPALIVE_ADDR = 0x20000000

# Full frames are appended here (marker addr len payload-hex) so large
# transfers such as an IR upload can be reassembled and analysed afterwards.
RAW_PATH = os.environ.get("BLACKBOX_RAW", "/tmp/blackbox-frames.log")
RAW = open(RAW_PATH, "a")


def _scan_wine_ports():
    out = subprocess.run(["aconnect", "-l"], capture_output=True, text=True).stdout
    found, client = [], None
    for line in out.splitlines():
        m = re.match(r"client (\d+): '([^']*)'", line)
        if m:
            client = m.group(1) if "WINE" in m.group(2).upper() else None
        elif client:
            p = re.match(r"\s+(\d+) '", line)
            if p and p.group(1) == "1":      # port 1 = what the app transmits
                found.append(f"{client}:1")
                client = None
    return found


def wine_port(wait_secs=0):
    """Find the Wine MIDI output port, refusing to guess between instances.

    Silently picking the first match is a real bug: with two copies of M-EFCS
    running, half the time you watch the instance the user is NOT driving and
    record nothing while believing the capture is live. Two instances also
    fight over the pedal and can wedge it mid-transfer. Fail loudly instead.

    The port only exists once the app is connected to a device, so wait_secs
    allows arming the capture before the user connects.
    """
    deadline = time.time() + wait_secs
    while True:
        found = _scan_wine_ports()
        if len(found) > 1:
            sys.exit(
                f"{len(found)} Wine MIDI clients are present: {', '.join(found)}\n"
                "More than one M-EFCS is running. They will fight over the pedal and\n"
                "the capture would watch an arbitrary one. Close all but the instance\n"
                "you are driving:\n"
                "    pkill -f m_efcs.exe    # then start exactly one"
            )
        if found:
            return found[0]
        if time.time() >= deadline:
            sys.exit("No WINE midi driver port - is M-EFCS running and connected?")
        time.sleep(1.0)


def main():
    port = wine_port(wait_secs=int(os.environ.get("BLACKBOX_WAIT", "0")))
    print(f"watching {port} (app -> pedal); keepalives hidden", file=sys.stderr, flush=True)
    proc = subprocess.Popen(["stdbuf", "-oL", "aseqdump", "-p", port],
                            stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            m = re.search(r"((?:[0-9A-F]{2} )+[0-9A-F]{2})", line)
            if not m or not m.group(1).startswith("F0"):
                continue
            raw = bytes(int(x, 16) for x in m.group(1).split())
            f = unpack7(raw[1:-1])
            if len(f) < 14 or int.from_bytes(f[0:2], "little") != MAGIC:
                continue
            marker = f[2]
            addr = int.from_bytes(f[7:11], "little")
            length = int.from_bytes(f[11:13], "little")
            if marker == MARK_READ and addr == KEEPALIVE_ADDR:
                continue
            payload = f[14:14 + length]
            kind = {MARK_READ: "READ ", MARK_WRITE: "WRITE"}.get(marker, f"M{marker:02X}  ")
            # Log the ENTIRE decoded frame, not just marker/addr/len/payload.
            # Offsets 5 and 13 are header fields this tool previously discarded;
            # they may carry the target bank/flags that distinguish a cab write
            # from a preset write, so never drop them again.
            if RAW:
                RAW.write(f"{marker:02X} {addr:08X} {length} "
                          f"b5={f[5]:02X} b13={f[13]:02X} frame={f.hex()}\n")
                RAW.flush()
            body = payload.hex(" ")
            if len(body) > 96:
                body = body[:96] + f" ... (+{length - 32}B)"
            print(f"{kind} addr=0x{addr:08X} len={length:<5} {body}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
