#!/usr/bin/env python3
"""Parse M-Vave AM4 amp models (GRU) and expose the pedal's fixed-point details.

Everything here was read out of amx_parser_lib.dll rather than guessed:

  hidden size 16      string "TODO : Only support 16 hiddens now", and the
                      caller passes edx=0x10
  weight scale        float * 2097151.0 (2**21-1) then truncate -> int32, i.e.
                      Q11.21 fixed point
  16 iterations       parser loop runs r9: 0 -> 0x440 step 0x44 (68 bytes,
                      17 int32 per unit)
  activations         512-entry piecewise-linear LUTs in .data, domain +-16.0
                      in Q21: sigmoid (slope 0x23000, intercept 0x23800,
                      output 0..2**23) and tanh (intercept 0x24000, slope
                      0x24800, output +-2**23). Both then >>2 to Q21.

Weight-block layout, offsets in bytes from the model base, float32 throughout.
Derived by mechanically extracting every load->store pair from the unrolled
parser loop, not by reading SSE by eye:

    0x000 + 4i          gate1 input weight        16
    0x040 + 4i          gate2 input weight        16
    0x080 + 4i          gate3 input weight        16
    0x0c0 + 64i + 4k    gate1 recurrent 16x16    256
    0x4c0 + 64i + 4k    gate2 recurrent 16x16    256
    0x8c0 + 64i + 4k    gate3 recurrent 16x16    256
    0xcc0 .. 0xe40      six further 16-float vectors (bias candidates)
                                                 ---
                                                 912 floats

Gate identity: gate3 is the odd one out -- the parser stores it in separate
arrays (64-byte stride) while gates 1 and 2 are interleaved 17-int32 rows.
In a GRU the candidate gate's recurrent product must be multiplied by the
reset gate before the nonlinearity, which is exactly why an implementation
keeps it apart. So gate3 is almost certainly the candidate (tanh) and gates
1/2 are reset and update (sigmoid). Which of 1/2 is reset vs update is NOT
yet established -- see validate() for how to settle it empirically instead of
guessing.
"""
import os
import struct
import sys

import numpy as np

HIDDEN = 16
WEIGHT_SCALE = 2097151.0          # 2**21 - 1
Q = 21                            # fractional bits
LUT_ENTRIES = 512
LUT_DOMAIN = 16.0                 # +-16.0 in Q21

# The vendor files are not part of this repo. Unpack the official M-EFCS
# Windows zip yourself and point $MEFCS_DIR at the folder that contains
# m_efcs.exe (default: extracted/M-EFCS next to tools/).
MEFCS_DIR = os.environ.get("MEFCS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extracted", "M-EFCS")
DLL = os.path.join(MEFCS_DIR, "amx_parser_lib.dll")
AMPCAB = os.path.join(MEFCS_DIR, "data", "flutter_assets", "lib", "resources",
                      "bin", "blackbox", "blackbox_am4_ampCab.bin")
DATA_VA, DATA_OFF = 0x180023000, 0x21600

LAYOUT = {
    "w_ih_g1": (0x000, HIDDEN, 1),
    "w_ih_g2": (0x040, HIDDEN, 1),
    "w_ih_g3": (0x080, HIDDEN, 1),
    "w_hh_g1": (0x0C0, HIDDEN, HIDDEN),
    "w_hh_g2": (0x4C0, HIDDEN, HIDDEN),
    "w_hh_g3": (0x8C0, HIDDEN, HIDDEN),
    "vec0":    (0xCC0, HIDDEN, 1),
    "vec1":    (0xD00, HIDDEN, 1),
    "vec2":    (0xD40, HIDDEN, 1),
    "vec3":    (0xD80, HIDDEN, 1),
    "vec4":    (0xDC0, HIDDEN, 1),
    "vec5":    (0xE00, HIDDEN, 1),
}


def load_luts(dll=DLL):
    """Extract the pedal's activation tables (slope, intercept) pairs."""
    d = open(dll, "rb").read()

    def tbl(va):
        off = DATA_OFF + (va - DATA_VA)
        return np.array(struct.unpack(f"<{LUT_ENTRIES}i",
                                      d[off:off + LUT_ENTRIES * 4]), dtype=np.int64)

    return {
        "sigmoid": (tbl(0x180023000), tbl(0x180023800)),
        "tanh":    (tbl(0x180024800), tbl(0x180024000)),
    }


def lut_eval(slope, intercept, x_q21):
    """Reproduce the pedal's PWL activation exactly, in integers.

    x_q21 is the accumulator in Q21. Mirrors the disassembly:
        v = x + 2**25 ; clamp ; idx = v>>17 ; frac = v & 0x1ffff
        out = ((slope[idx]*frac)>>17 + intercept[idx]) >> 2
    """
    v = np.asarray(x_q21, dtype=np.int64) + (1 << 25)
    v = np.clip(v, 0, (LUT_ENTRIES << 17) - 1)
    idx = (v >> 17).astype(np.int64)
    frac = v & 0x1FFFF
    return ((slope[idx] * frac >> 17) + intercept[idx]) >> 2


def parse(record, base=0):
    """Split an AM4 weight block into named float32 arrays."""
    out = {}
    for name, (off, rows, cols) in LAYOUT.items():
        n = rows * cols
        raw = record[base + off: base + off + n * 4]
        if len(raw) < n * 4:
            raise ValueError(f"record too short for {name} at 0x{off:X}")
        a = np.array(struct.unpack(f"<{n}f", raw), dtype=np.float64)
        out[name] = a.reshape(rows, cols) if cols > 1 else a
    return out


# RESOLVED by measurement against the pedal (tools/gru_test.py).
#
# Weight base is 0x67C. The competing candidate 0xABC came from the "LMT"
# anchor (calc_bias_gru checks for it at model+0x1400, and "LMT\0" sits at file
# offset 0x1ebc) -- that reasoning assumed the file record is copied verbatim
# into the runtime buffer, and it is not.
#
# Gate order is gate1 = reset, gate2 = update, gate3 = candidate.
#
# Evidence, scoring a linear readout out-of-sample against a real (dry, wet)
# capture -- only the correct combination generalises at all:
#
#   base 0x67C  g1=r,g2=z   R2 +0.825      <-- correct
#   base 0xABC  g1=z,g2=r   R2 -1.212
#   base 0xABC  g1=r,g2=z   R2 -1.306
#   base 0x67C  g1=z,g2=r   R2 -1.395
#   scrambled weights, n=5  R2 -0.412 (max +0.156)
#
# And it is specific to the model actually loaded: with the same base and
# order, the amp record the pedal was using scores +0.825 while records
# 1,2,3,7,12,19 all score negative.
WEIGHT_BASE = 0x67C
GATE_RESET, GATE_UPDATE, GATE_CANDIDATE = 0, 1, 2

WEIGHT_BASE_CANDIDATES = {"balanced": 0x67C, "lmt_anchor": 0xABC}


def find_weight_base(record=None, which="balanced"):
    """Weight base within an AM4 file record. Established by measurement."""
    return WEIGHT_BASE_CANDIDATES[which]


def summary(path, index=0, rec=8192):
    d = open(path, "rb").read()
    r = d[index * rec:(index + 1) * rec]
    name = r[4:18].split(b"\0")[0].decode("ascii", "replace")
    base = find_weight_base(r)
    print(f"{path.split('/')[-1]} record {index}: {r[:4]!r} name={name!r} "
          f"weight base=0x{base:X}")
    try:
        w = parse(r, base)
    except ValueError as e:
        print("  ", e)
        return
    for k, v in w.items():
        print(f"  {k:9s} shape={str(v.shape):9s} mean={v.mean():+.5f} "
              f"sd={v.std():.5f} min={v.min():+.4f} max={v.max():+.4f}")


if __name__ == "__main__":
    luts = load_luts()
    for name, (s, i) in luts.items():
        print(f"{name:8s} slope[256]={s[256]:>10} intercept[256]={i[256]:>10} "
              f"range={i.min()}..{i.max()}")
    print()
    p = sys.argv[1] if len(sys.argv) > 1 else AMPCAB
    summary(p, int(sys.argv[2]) if len(sys.argv) > 2 else 0)
