#!/usr/bin/env python3
"""Load a cabinet impulse response onto the M-Vave Blackbox from Linux.

The pedal stores a cab IR as a `CAB` record: a 20-byte header followed by
2048 little-endian 24-bit PCM samples at 44.1 kHz. Verified by capturing the
vendor editor's own upload and confirming the bytes it sent were identical to
the source WAV, sample for sample.

    CAB record (6164 bytes)
      0..3    "CAB\\0"
      4..17   name (14 bytes)
      18      format flag = 1
      19      cab level   = 0..100
      20..    2048 * int24 LE

Upload sequence, replicating the editor (every frame carries cmd=0x05, the
cab memory space -- with cmd=0x04 these same addresses are preset flash):

    WRITE 0xA0000000 <- slot        select target cab slot
    WRITE 0x00000000 <- payload     in 1015-byte chunks
    WRITE 0x00000000 <- header      20 bytes, name + level
    WRITE 0xF0000100 <- 00          commit (low bits vary between captures)

Usage:
    python3 irload.py cab.wav --slot 2            # dry run, prints what it would do
    python3 irload.py cab.wav --slot 2 --yes      # actually write
"""
import argparse
import os
import struct
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blackbox import Pedal, REC, CMD_CAB  # noqa: E402

IR_SAMPLES = 2048
IR_RATE = 44100
CHUNK = 1015
REGION_SELECT = 0xA0000000
CAB_COMMIT = 0xF0000100
NAME_LEN = 14


def read_wav(path):
    """Return (mono float array, sample rate). Handles PCM 8/16/24/32 and float32."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        sys.exit(f"{path}: not a RIFF/WAVE file")

    fmt = None
    pos = 12
    data = None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        sys.exit(f"{path}: missing fmt or data chunk")

    tag, channels, rate, _, _, bits = fmt
    if tag == 3 and bits == 32:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float64)
    elif tag in (1, 0xFFFE):
        if bits == 8:
            samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float64) - 128) / 128.0
        elif bits == 16:
            samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
        elif bits == 24:
            b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - (1 << 24), v)
            samples = v.astype(np.float64) / 8388608.0
        elif bits == 32:
            samples = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
        else:
            sys.exit(f"{path}: unsupported bit depth {bits}")
    else:
        sys.exit(f"{path}: unsupported WAV format tag {tag}")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def to_ir(samples, rate, normalize=True):
    """Resample to 44.1 kHz and fit to exactly 2048 samples."""
    notes = []
    if rate != IR_RATE:
        n = int(round(len(samples) * IR_RATE / rate))
        samples = np.interp(np.linspace(0, len(samples) - 1, n),
                            np.arange(len(samples)), samples)
        notes.append(f"resampled {rate} -> {IR_RATE} Hz")
    if len(samples) > IR_SAMPLES:
        notes.append(f"truncated {len(samples)} -> {IR_SAMPLES} samples "
                     f"({(len(samples)-IR_SAMPLES)/IR_RATE*1000:.0f} ms of tail dropped)")
        samples = samples[:IR_SAMPLES]
    elif len(samples) < IR_SAMPLES:
        notes.append(f"zero-padded {len(samples)} -> {IR_SAMPLES} samples")
        samples = np.pad(samples, (0, IR_SAMPLES - len(samples)))
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if normalize and peak > 0:
        samples = samples / peak
        notes.append(f"normalised (source peak {peak:.4f})")
    elif peak > 1.0:
        samples = np.clip(samples, -1.0, 1.0)
        notes.append("clipped to full scale")
    return samples, notes


def build_cab(samples, name, level):
    # Scale by 2**23, not 2**23-1: that is what the editor uses, and it makes
    # a 24-bit source round-trip to its exact original integers (verified
    # against a captured upload, 0 of 2048 samples differing).
    q = np.clip(np.round(samples * 8388608.0), -8388608, 8388607).astype(np.int32)
    body = bytearray()
    for v in q:
        body += int(v).to_bytes(3, "little", signed=True)
    nm = name.encode("ascii", "replace")[:NAME_LEN]
    header = b"CAB\0" + nm + b"\0" * (NAME_LEN - len(nm)) + bytes([1, level & 0xFF])
    assert len(header) == 20, len(header)
    return bytes(header) + bytes(body)


def upload(pedal, record, slot):
    """Replicate the editor's upload, in the cab memory space.

    CRITICAL: every frame must carry cmd=CMD_CAB (0x05). Byte 6 of the frame
    selects the memory space, and the addresses below repeat across spaces --
    address 0 with cmd=0x04 is *preset slot 0*. Sending these writes with the
    preset command overwrites preset flash (a 6164-byte record covers preset
    slots 0..66). That is not hypothetical; it happened twice before the
    command byte was understood.
    """
    # The editor streams the body with the name field blank, then sets the
    # real header afterwards. Match that so the transfer is byte-identical.
    staged = bytearray(record)
    staged[4:18] = b"\0" * NAME_LEN
    for off in range(0, len(staged), CHUNK):
        pedal.write_space(off, bytes(staged[off:off + CHUNK]), CMD_CAB)
    pedal.write_space(REGION_SELECT, bytes([slot & 0xFF]), CMD_CAB)
    pedal.write_space(0x00000000, record[:20], CMD_CAB)
    pedal.write_space(CAB_COMMIT, bytes([0x00]), CMD_CAB)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wav")
    ap.add_argument("--slot", type=int, required=True, help="cab slot index")
    ap.add_argument("--name", help="name stored on the pedal (default: filename)")
    ap.add_argument("--level", type=int, default=50, help="cab level 0-100 (default 50)")
    # The editor does NOT normalise - it preserves the source level, and with
    # normalisation off our record is byte-identical to its upload. Keep that
    # as the default so IRs sound exactly as their author intended; scaling is
    # opt-in.
    ap.add_argument("--normalize", action="store_true",
                    help="scale to full scale (the editor does not do this)")
    ap.add_argument("--out", help="also write the CAB record to this file")
    ap.add_argument("--yes", action="store_true", help="actually write to the pedal")
    ap.add_argument("-p", "--port")
    args = ap.parse_args()

    samples, rate = read_wav(args.wav)
    print(f"{os.path.basename(args.wav)}: {len(samples)} samples @ {rate} Hz")
    samples, notes = to_ir(samples, rate, normalize=args.normalize)
    for n in notes:
        print(f"  - {n}")

    name = args.name or os.path.splitext(os.path.basename(args.wav))[0]
    if len(name) > NAME_LEN:
        print(f"  - name truncated to {NAME_LEN} chars: {name[:NAME_LEN]!r}")
    rec = build_cab(samples, name, args.level)
    print(f"\nCAB record: {len(rec)} bytes  name={name[:NAME_LEN]!r} level={args.level}")
    print(f"  header: {rec[:20].hex(' ')}")

    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(rec)
        print(f"  written to {args.out}")

    if not args.yes:
        print(f"\ndry run - would upload to cab slot {args.slot} "
              f"in {-(-len(rec)//CHUNK)} chunks; re-run with --yes")
        return

    with Pedal(args.port) as p:
        upload(p, rec, args.slot)
    print(f"\nuploaded to cab slot {args.slot}")


if __name__ == "__main__":
    main()
