#!/usr/bin/env python3
"""Brute-force the trailing checksum of M-Vave Blackbox SysEx frames.

Corpus = frames observed on the wire between M-EFCS and the pedal, all of
which the device accepted (the heartbeat request provably round-trips).
Hypothesis under test: the last two bytes before F7 encode a checksum over
some prefix of the frame, split into 7-bit halves (SysEx-safe).
"""

FRAMES = [
    ("hb_req",  "F0 00 32 0D 41 00 00 00 02 00 00 00 00 42 00 00 00 57 01 F7"),
    ("cab_req", "F0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 00 44 03 F7"),
    ("vol_set", "F0 00 32 09 51 00 00 00 02 00 00 00 00 21 00 00 00 64 00 14 04 F7"),
    ("hb_resp", "F0 00 32 0D 61 00 00 00 02 00 00 00 00 42 00 00 00 14 28 04 00 60 15 F7"),
    ("ack",     "F0 00 32 01 08 00 00 00 00 7F 01 F7"),
]

def parse(h):
    return bytes(int(x, 16) for x in h.split())

CORPUS = [(n, parse(h)) for n, h in FRAMES]


def targets(frame, cs_len=2):
    """(body, expected) for a frame, stripping F0 .. F7 and the checksum."""
    inner = frame[1:-1]                  # drop F0 / F7
    return inner[:-cs_len], inner[-cs_len:]


def as14(cs):
    """Two 7-bit bytes -> 14-bit int, both orders."""
    return [cs[0] | (cs[1] << 7), cs[1] | (cs[0] << 7)]


def crc16(data, poly, init, refin, refout):
    def rev8(b):
        return int(f"{b:08b}"[::-1], 2)
    crc = init
    for b in data:
        if refin:
            b = rev8(b)
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    if refout:
        crc = int(f"{crc:016b}"[::-1], 2)
    return crc


def crc8(data, poly, init):
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def try_simple(skip):
    """Sum / XOR style checksums over body[skip:]."""
    hits = []
    for name, fn in [
        ("sum",        lambda d: sum(d)),
        ("sum_neg",    lambda d: (-sum(d)) & 0x3FFF),
        ("xor",        lambda d: __import__('functools').reduce(lambda a, b: a ^ b, d, 0)),
        ("fletcher",   lambda d: _fletcher(d)),
    ]:
        ok = True
        for _, fr in CORPUS:
            body, cs = targets(fr)
            body = body[skip:]
            if fn(body) not in as14(cs):
                ok = False
                break
        if ok:
            hits.append(f"{name} (skip={skip})")
    return hits


def _fletcher(d):
    a = b = 0
    for x in d:
        a = (a + x) % 255
        b = (b + a) % 255
    return (b << 8) | a


def main():
    print("corpus:", ", ".join(n for n, _ in CORPUS))
    for skip in range(0, 5):
        for h in try_simple(skip):
            print("  SIMPLE HIT:", h)

    print("\nbrute-forcing CRC16 (poly x init x refin x refout x skip)...")
    found = []
    for skip in (0, 1, 2, 3):
        bodies = []
        for _, fr in CORPUS:
            body, cs = targets(fr)
            bodies.append((body[skip:], as14(cs)))
        for poly in range(0x10000):
            for init in (0x0000, 0xFFFF):
                for refin in (False, True):
                    for refout in (False, True):
                        ok = True
                        for body, want in bodies:
                            if crc16(body, poly, init, refin, refout) not in want:
                                ok = False
                                break
                        if ok:
                            found.append((skip, poly, init, refin, refout))
                            print(f"  CRC16 HIT skip={skip} poly=0x{poly:04X} "
                                  f"init=0x{init:04X} refin={refin} refout={refout}")
    if not found:
        print("  no CRC16 match")

    print("\nbrute-forcing CRC8 on the low byte only...")
    for skip in (0, 1, 2, 3):
        for poly in range(0x100):
            for init in (0x00, 0xFF):
                ok = True
                for _, fr in CORPUS:
                    body, cs = targets(fr)
                    if crc8(body[skip:], poly, init) & 0x7F != cs[0]:
                        ok = False
                        break
                if ok:
                    print(f"  CRC8 HIT skip={skip} poly=0x{poly:02X} init=0x{init:02X}")


if __name__ == "__main__":
    main()
