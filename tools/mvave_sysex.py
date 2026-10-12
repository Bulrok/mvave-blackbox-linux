#!/usr/bin/env python3
"""M-Vave (Sinco/Jieli) SysEx codec for the Blackbox and siblings.

Transport, as implemented by the vendor's M-UPGRADE.exe and M-EFCS:

  F0 <payload re-packed 8-bit -> 7-bit, LSB first> F7

The packer treats the 8-bit frame as a continuous little-endian bitstream and
emits it 7 bits at a time, so SysEx's "data bytes must be < 0x80" holds without
escaping. Decoding is the same operation in reverse.

The inner 8-bit frame is:

  off 0..1  u16  magic
  off 2     u8   0x30 marker
  off 3..4  u16  len + 8
  off 5     u8   (sub-target / flags)
  off 6     u8   command            <-- checksum starts here
  off 7..10 u32  address / parameter id
  off 11..12 u16 payload length
  off 13    u8   (flags)
  off 14..  u8[] payload
  last      u8   checksum = ~(sum of bytes from off 6) & 0xFF
"""

def unpack7(data7):
    """7-bit SysEx body -> 8-bit frame (LSB-first bitstream)."""
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


def pack7(data8):
    """8-bit frame -> 7-bit SysEx body (LSB-first bitstream)."""
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


def checksum(frame8):
    """~sum over frame[6:], as the vendor's verifier computes it."""
    return (~sum(frame8[6:])) & 0xFF


def decode(sysex):
    """Full F0..F7 message -> 8-bit frame."""
    assert sysex[0] == 0xF0 and sysex[-1] == 0xF7, "not a SysEx message"
    return unpack7(sysex[1:-1])


def encode(frame8):
    """8-bit frame -> full F0..F7 message."""
    return bytes([0xF0]) + pack7(frame8) + bytes([0xF7])


def describe(frame8):
    if len(frame8) < 14:
        return f"  (short frame, {len(frame8)} bytes)"
    magic = int.from_bytes(frame8[0:2], 'little')
    marker = frame8[2]
    lenp8 = int.from_bytes(frame8[3:5], 'little')
    b5 = frame8[5]
    cmd = frame8[6]
    addr = int.from_bytes(frame8[7:11], 'little')
    plen = int.from_bytes(frame8[11:13], 'little')
    b13 = frame8[13]
    payload = frame8[14:14 + plen]
    cs_at = 14 + plen
    cs = frame8[cs_at] if cs_at < len(frame8) else None
    calc = (~sum(frame8[6:cs_at])) & 0xFF
    return (f"  magic=0x{magic:04X} marker=0x{marker:02X} len+8={lenp8} b5=0x{b5:02X}\n"
            f"  cmd=0x{cmd:02X} addr=0x{addr:08X} plen={plen} b13=0x{b13:02X}\n"
            f"  payload={payload.hex(' ') or '(none)'}\n"
            f"  checksum: got={'--' if cs is None else f'0x{cs:02X}'} calc=0x{calc:02X} "
            f"{'OK' if cs == calc else 'MISMATCH'}")


CAPTURED = [
    ("hb_req  (app->dev)", "F0 00 32 0D 41 00 00 00 02 00 00 00 00 42 00 00 00 57 01 F7"),
    ("hb_resp (dev->app)", "F0 00 32 0D 61 00 00 00 02 00 00 00 00 42 00 00 00 14 28 04 00 60 15 F7"),
    ("cab_list_req",       "F0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 00 44 03 F7"),
    ("ack",                "F0 00 32 01 08 00 00 00 00 7F 01 F7"),
    ("vol_set_100",        "F0 00 32 09 51 00 00 00 02 00 00 00 00 21 00 00 00 64 00 14 04 F7"),
]

if __name__ == "__main__":
    for name, hexstr in CAPTURED:
        raw = bytes(int(x, 16) for x in hexstr.split())
        f8 = decode(raw)
        print(f"=== {name} ===")
        print(f"  sysex ({len(raw)}B): {hexstr}")
        print(f"  frame ({len(f8)}B): {f8.hex(' ')}")
        print(describe(f8))
        # round-trip check
        assert pack7(f8)[:len(raw) - 2] == raw[1:-1][:len(raw) - 2], "round-trip failed"
        print()
