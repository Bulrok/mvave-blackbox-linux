#!/usr/bin/env python3
"""blackbox - native Linux control for the M-Vave Blackbox multi-effects pedal.

The vendor ships Windows/macOS only. The pedal itself speaks a memory-mapped
read/write protocol over ordinary USB-MIDI SysEx, which Linux exposes natively,
so no vendor software is required for parameter control.

Protocol (reverse engineered from M-EFCS / M-UPGRADE, see PROTOCOL.md):

  SysEx:  F0 <8-bit frame re-packed to 7 bits, LSB-first bitstream> F7
  Frame:  u16 magic=0x5900 | u8 marker | u16 payload_len+8 | u8 pad | u8 cmd=0x04
          | u32 address | u16 length | u8 flags | payload... | u8 checksum
  marker: 0x23 = read, 0x22 = write
  checksum = ~(sum of frame[6:]) & 0xFF

Transport uses the ALSA sequencer via aplaymidi/aseqdump, so it coexists with
PipeWire holding the raw MIDI node (no extra Python dependencies).
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time

MAGIC = 0x5900
MARK_READ = 0x23
MARK_WRITE = 0x22

# Byte 6 selects the MEMORY SPACE, it is not a constant. Addresses repeat
# across spaces: address 0 with CMD_PRESET is preset slot 0, and address 0
# with CMD_CAB is cab storage. Getting this wrong writes IR data over preset
# flash, which is exactly what happened before it was understood.
CMD_PRESET = 0x04      # presets, working buffer, status
CMD_CAB = 0x05         # cab/IR storage and its name tables
CMD = CMD_PRESET       # default for preset-space operations

# Working preset buffer: one 92-byte record at 0x10000000, the same record the
# editor reads. Flash slot N is at N * 92 and is directly readable.
REC = 92
WORKING = 0x10000000
STATUS = 0x20000000
COMMIT = 0xF0000000          # zero-length write = flush to flash
SLOT_VALID = 0xE0000000      # + slot index, payload 01

BLOCKS = ["FX", "AMP", "CAB", "MOD", "REV", "DLY"]
BLOCK_ENABLE = {n: WORKING + 8 + i for i, n in enumerate(BLOCKS)}

NAMED = {
    "vol":     (WORKING + 0x00, 2),
    "fx":      (BLOCK_ENABLE["FX"], 1),
    "amp":     (BLOCK_ENABLE["AMP"], 1),
    "cab":     (BLOCK_ENABLE["CAB"], 1),
    "mod":     (BLOCK_ENABLE["MOD"], 1),
    "rev":     (BLOCK_ENABLE["REV"], 1),
    "dly":     (BLOCK_ENABLE["DLY"], 1),
    "fxtype":  (WORKING + 0x0E, 1),
    "amptype": (WORKING + 0x0F, 1),
    "status":  (STATUS, 4),
}


# ---------------------------------------------------------------- codec

def pack7(data8):
    acc = bits = 0
    out = bytearray()
    for b in data8:
        acc |= b << bits
        bits += 8
        while bits >= 7:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    if bits:
        out.append(acc & 0x7F)
    return bytes(out)


def unpack7(data7):
    acc = bits = 0
    out = bytearray()
    for b in data7:
        acc |= (b & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    return bytes(out)


def build(marker, addr, length, payload=b"", cmd=CMD):
    f = bytearray()
    f += MAGIC.to_bytes(2, "little")
    f += bytes([marker])
    f += (len(payload) + 8).to_bytes(2, "little")
    f += bytes([0x00, cmd])
    f += addr.to_bytes(4, "little")
    f += length.to_bytes(2, "little")
    f += bytes([0x00])
    f += payload
    f.append((~sum(f[6:])) & 0xFF)
    return bytes(f)


def parse(sysex):
    """F0..F7 -> (marker, addr, length, payload) or None if malformed."""
    if len(sysex) < 4 or sysex[0] != 0xF0 or sysex[-1] != 0xF7:
        return None
    f = unpack7(sysex[1:-1])
    if len(f) < 14 or int.from_bytes(f[0:2], "little") != MAGIC:
        return None
    addr = int.from_bytes(f[7:11], "little")
    length = int.from_bytes(f[11:13], "little")
    return f[2], addr, length, f[14:14 + length]


# ---------------------------------------------------------------- transport

class Pedal:
    """Talks to the pedal over the ALSA sequencer."""

    def __init__(self, port=None, timeout=1.5):
        self.port = port or self.find_port()
        self.timeout = timeout
        self._tmp = tempfile.mkdtemp(prefix="blackbox-")
        self._dump = None

    @staticmethod
    def find_port():
        """Locate the pedal's ALSA sequencer port."""
        try:
            out = subprocess.run(["aconnect", "-l"], capture_output=True,
                                 text=True, check=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            sys.exit("error: aconnect not available (install alsa-utils)")
        client = None
        for line in out.splitlines():
            m = re.match(r"client (\d+): '([^']*)'", line)
            if m:
                client = m.group(1) if "Composite" in m.group(2) else None
            elif client and re.match(r"\s+(\d+) '", line):
                return f"{client}:{re.match(r'\s+(\d+) ', line).group(1)}"
        sys.exit("error: Blackbox not found. Is it plugged in? (check `aconnect -l`)")

    def __enter__(self):
        self._log = os.path.join(self._tmp, "rx.log")
        self._fh = open(self._log, "wb")
        self._dump = subprocess.Popen(["stdbuf", "-oL", "aseqdump", "-p", self.port],
                                      stdout=self._fh, stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.getsize(self._log):
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if self._dump:
            self._dump.terminate()
            self._dump.wait()
        self._fh.close()

    def _rx_lines(self):
        with open(self._log, "rb") as fh:
            return fh.read().decode("utf-8", "replace").splitlines()

    def read_space(self, addr, length, cmd):
        got = self.send(build(MARK_READ, addr, length, cmd=cmd))
        return got[3] if got else None

    def write_space(self, addr, payload, cmd):
        self.send(build(MARK_WRITE, addr, len(payload), payload, cmd=cmd),
                  expect=False)

    def send(self, frame, expect=True):
        """Send a frame; return the first well-formed reply, or None."""
        before = len(self._rx_lines())
        sysex = bytes([0xF0]) + pack7(frame) + bytes([0xF7])
        mid = os.path.join(self._tmp, "out.mid")
        write_smf(mid, sysex)
        subprocess.run(["aplaymidi", "-p", self.port, mid],
                       capture_output=True, check=False)
        if not expect:
            time.sleep(0.15)
            return None
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            for line in self._rx_lines()[before:]:
                m = re.search(r"((?:[0-9A-F]{2} )+[0-9A-F]{2})", line)
                if m and m.group(1).startswith("F0"):
                    got = parse(bytes(int(x, 16) for x in m.group(1).split()))
                    if got:
                        return got
            time.sleep(0.05)
        return None

    def read(self, addr, length):
        got = self.send(build(MARK_READ, addr, length))
        return got[3] if got else None

    def write(self, addr, payload):
        self.send(build(MARK_WRITE, addr, len(payload), payload), expect=False)

    def program_change(self, prog, chan=0):
        """Switch preset. The pedal follows ordinary MIDI Program Change."""
        trk = bytes([0x00, 0xC0 | chan, prog]) + b"\x00\xFF\x2F\x00"
        path = os.path.join(self._tmp, "pc.mid")
        with open(path, "wb") as fh:
            fh.write(b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") +
                     (1).to_bytes(2, "big") + (96).to_bytes(2, "big") +
                     b"MTrk" + len(trk).to_bytes(4, "big") + trk)
        subprocess.run(["aplaymidi", "-p", self.port, path], capture_output=True)
        time.sleep(0.8)

    def current_slot(self):
        d = self.read(STATUS, 4)
        return d[0] if d else None

    def read_slot(self, idx):
        return self.read(idx * REC, REC)

    def save_slot(self, idx, record):
        """Commit a 92-byte record to flash slot idx, as the editor does."""
        if len(record) != REC:
            raise ValueError(f"record must be {REC} bytes, got {len(record)}")
        self.write(idx * REC, record)
        self.write(COMMIT, b"")
        self.write(SLOT_VALID + idx, bytes([0x01]))
        time.sleep(0.4)


def write_smf(path, sysex):
    """Minimal type-0 SMF carrying one SysEx event (for aplaymidi)."""
    def varlen(n):
        out = bytearray([n & 0x7F])
        n >>= 7
        while n:
            out.insert(0, (n & 0x7F) | 0x80)
            n >>= 7
        return bytes(out)

    body = sysex[1:]
    trk = b"\x00\xF0" + varlen(len(body)) + body + b"\x00\xFF\x2F\x00"
    with open(path, "wb") as fh:
        fh.write(b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") +
                 (1).to_bytes(2, "big") + (96).to_bytes(2, "big") +
                 b"MTrk" + len(trk).to_bytes(4, "big") + trk)


# ---------------------------------------------------------------- commands

def parse_addr(s):
    if s in NAMED:
        return NAMED[s]
    return int(s, 0), None


def cmd_read(args):
    addr, deflen = parse_addr(args.address)
    length = args.length or deflen or 4
    with Pedal(args.port) as p:
        data = p.read(addr, length)
    if data is None:
        sys.exit("no response from pedal")
    print(f"0x{addr:08X}  {data.hex(' ')}  = {int.from_bytes(data, 'little')}")


def cmd_write(args):
    addr, deflen = parse_addr(args.address)
    if re.fullmatch(r"\d+", args.value):
        payload = int(args.value).to_bytes(deflen or 2, "little")
    else:
        payload = bytes.fromhex(args.value.replace(" ", ""))
    with Pedal(args.port) as p:
        p.write(addr, payload)
        back = p.read(addr, len(payload))
    print(f"wrote 0x{addr:08X} = {payload.hex(' ')}"
          + (f"; read back {back.hex(' ')}" if back else "; (no read-back)"))


def cmd_status(args):
    with Pedal(args.port) as p:
        data = p.read(0x20000000, 4)
    if data is None:
        sys.exit("no response from pedal")
    print(f"status: {data.hex(' ')}")


def cmd_scan(args):
    """Read-only sweep of an address range to discover live parameters."""
    start, end, step = int(args.start, 0), int(args.end, 0), args.step
    hits = 0
    with Pedal(args.port, timeout=args.timeout) as p:
        for addr in range(start, end + 1, step):
            data = p.read(addr, args.length)
            if data:
                hits += 1
                print(f"0x{addr:08X}  {data.hex(' ')}  = "
                      f"{int.from_bytes(data, 'little')}", flush=True)
            elif args.verbose:
                print(f"0x{addr:08X}  --", flush=True)
    print(f"\n{hits} address(es) responded", file=sys.stderr)


def cmd_monitor(args):
    """Print decoded frames seen on the pedal's port."""
    port = args.port or Pedal.find_port()
    print(f"monitoring {port} (Ctrl-C to stop)", file=sys.stderr)
    proc = subprocess.Popen(["stdbuf", "-oL", "aseqdump", "-p", port],
                            stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            m = re.search(r"((?:[0-9A-F]{2} )+[0-9A-F]{2})", line)
            if not m or not m.group(1).startswith("F0"):
                continue
            got = parse(bytes(int(x, 16) for x in m.group(1).split()))
            if not got:
                continue
            marker, addr, length, payload = got
            kind = {MARK_READ: "READ ", MARK_WRITE: "WRITE"}.get(marker, f"0x{marker:02X}")
            print(f"{kind} 0x{addr:08X} len={length:<4} {payload.hex(' ')}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


def show_record(rec, label):
    print(f"{label}")
    print(f"  volume    {rec[0]}")
    print(f"  chain     " + "  ".join(
        f"{n}:{'on ' if rec[8 + i] else 'off'}" for i, n in enumerate(BLOCKS)))
    print(f"  fx type   {rec[0x0E]}")
    print(f"  amp       {rec[0x0F]}")


def cmd_chain(args):
    with Pedal(args.port) as p:
        if args.slot is None:
            rec = p.read(WORKING, REC)
            label = f"working buffer (preset {(p.current_slot() or 0) + 1})"
        else:
            rec = p.read_slot(args.slot)
            label = f"flash slot {args.slot} (preset {args.slot + 1})"
    if not rec:
        sys.exit("no response from pedal")
    show_record(rec, label)


def cmd_preset(args):
    with Pedal(args.port) as p:
        p.program_change(args.number - 1)
        cur = p.current_slot()
    print(f"switched to preset {args.number}"
          + (f" (device reports index {cur})" if cur is not None else ""))


def cmd_backup(args):
    data = bytearray()
    with Pedal(args.port, timeout=0.6) as p:
        for idx in range(args.slots):
            rec = p.read_slot(idx)
            if not rec:
                print(f"slot {idx}: no reply, stopping", file=sys.stderr)
                break
            data += rec
    with open(args.file, "wb") as fh:
        fh.write(data)
    print(f"backed up {len(data)//REC} slots -> {args.file} ({len(data)} bytes)")


def cmd_restore(args):
    blob = open(args.file, "rb").read()
    if len(blob) % REC:
        sys.exit(f"{args.file} is not a whole number of {REC}-byte records")
    total = len(blob) // REC
    targets = range(total) if args.slot is None else [args.slot]
    if not args.yes:
        sys.exit(f"would write {len(list(targets))} slot(s) to flash from {args.file}; "
                 f"re-run with --yes to actually do it")
    with Pedal(args.port) as p:
        for idx in targets:
            p.save_slot(idx, blob[idx * REC:(idx + 1) * REC])
            print(f"restored slot {idx}", flush=True)
    print("done")


def cmd_save(args):
    """Commit the current working buffer into a flash slot."""
    with Pedal(args.port) as p:
        rec = p.read(WORKING, REC)
        if not rec:
            sys.exit("no response from pedal")
        show_record(rec, "working buffer")
        if not args.yes:
            sys.exit(f"\nwould write the above into flash slot {args.slot} "
                     f"(preset {args.slot + 1}); re-run with --yes")
        p.save_slot(args.slot, rec)
        back = p.read_slot(args.slot)
    ok = back is not None and bytes(back) == bytes(rec)
    print(f"\nsaved to slot {args.slot}; read-back {'matches' if ok else 'DIFFERS'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-p", "--port", help="ALSA sequencer port (default: autodetect)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="read device status word")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("read", help="read an address")
    s.add_argument("address", help="hex address or name (%s)" % ", ".join(NAMED))
    s.add_argument("length", nargs="?", type=int)
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("write", help="write an address")
    s.add_argument("address")
    s.add_argument("value", help="decimal value, or raw hex bytes")
    s.set_defaults(func=cmd_write)

    s = sub.add_parser("scan", help="read-only sweep to discover parameters")
    s.add_argument("start")
    s.add_argument("end")
    s.add_argument("--step", type=int, default=4)
    s.add_argument("--length", type=int, default=2)
    s.add_argument("--timeout", type=float, default=0.4)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("monitor", help="decode live traffic on the pedal port")
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("chain", help="show the effect chain")
    s.add_argument("--slot", type=int, help="read a flash slot instead of the live buffer")
    s.set_defaults(func=cmd_chain)

    s = sub.add_parser("preset", help="switch preset (1-based, via Program Change)")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_preset)

    s = sub.add_parser("backup", help="dump all flash slots to a file")
    s.add_argument("file")
    s.add_argument("--slots", type=int, default=64)
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="write slots back from a backup file")
    s.add_argument("file")
    s.add_argument("--slot", type=int, help="restore only this slot")
    s.add_argument("--yes", action="store_true", help="actually write flash")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("save", help="commit the working buffer into a flash slot")
    s.add_argument("slot", type=int)
    s.add_argument("--yes", action="store_true", help="actually write flash")
    s.set_defaults(func=cmd_save)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
