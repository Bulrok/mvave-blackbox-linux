#!/usr/bin/env python3
"""Discriminate AM4 weight-base and gate-ordering candidates against a capture.

Two things about the AM4 GRU are unresolved (see PROTOCOL.md):

  weight base   0xABC (from the "LMT" anchor) or 0x67C (best statistical
                balance of the three gate matrices)
  gate order    gate3 is almost certainly the candidate/tanh gate, but which
                of gate1/gate2 is reset vs update is unknown

That is 2 x 2 = 4 combinations. This runs each forward over a real (dry, wet)
capture from amprig.py and asks which reproduces the pedal.

Method. The readout stage (hidden -> output sample) is NOT mapped, so it is
fitted by least squares from the hidden trajectory to the wet signal, for every
candidate equally. A candidate with the right internal representation supports
an accurate linear readout; a wrong one does not. A scrambled-weights control
shows what chance looks like, so a mediocre winner cannot be mistaken for a
real one.

This is a discriminator, not a proof of exactness: the pedal also applies a
tone stack and level stages that are not modelled here.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from am4 import AMPCAB, HIDDEN, LAYOUT, WEIGHT_BASE_CANDIDATES, parse  # noqa: E402


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


def gru_forward(x, w_ih, w_hh, b_ih, b_hh, order):
    """Standard GRU, input size 1, hidden 16. Returns hidden trajectory (T,16).

    order maps ('r','z','n') onto gate slots 0..2.
    """
    ri, zi, ni = order
    T = len(x)
    H = np.empty((T, HIDDEN), dtype=np.float64)
    h = np.zeros(HIDDEN, dtype=np.float64)
    for t in range(T):
        xi = x[t]
        gi = w_ih * xi + b_ih          # (3,16)
        gh = w_hh @ h + b_hh           # (3,16)
        r = sigmoid(gi[ri] + gh[ri])
        z = sigmoid(gi[zi] + gh[zi])
        n = np.tanh(gi[ni] + r * gh[ni])
        h = (1.0 - z) * n + z * h
        H[t] = h
    return H


def readout_fit(H, x, y, split=0.5):
    """Linear readout hidden(+input,+bias) -> y, fitted on the first `split`
    of the data and scored on the held-out remainder.

    The readout has 18 free parameters, so an in-sample R2 partly measures
    fitting freedom rather than whether the representation is right. Scoring
    out-of-sample removes that.
    """
    A = np.column_stack([H, x, np.ones(len(x))])
    k = int(len(y) * split)
    coef, *_ = np.linalg.lstsq(A[:k], y[:k], rcond=None)
    resid = y[k:] - A[k:] @ coef
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y[k:] - y[k:].mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    nrms = float(np.sqrt(np.mean(resid ** 2)) / (np.sqrt(np.mean(y[k:] ** 2)) or 1))
    return r2, nrms


def load_weights(base, amp_index):
    rec = open(AMPCAB, "rb").read()[amp_index * 8192:(amp_index + 1) * 8192]
    if rec[:4] != b"AM4\0":
        sys.exit(f"record {amp_index} is not AM4")
    w = parse(rec, base)
    w_ih = np.stack([w["w_ih_g1"], w["w_ih_g2"], w["w_ih_g3"]])          # (3,16)
    w_hh = np.stack([w["w_hh_g1"], w["w_hh_g2"], w["w_hh_g3"]])          # (3,16,16)
    b_ih = np.stack([w["vec0"], w["vec1"], w["vec2"]])
    b_hh = np.stack([w["vec3"], w["vec4"], w["vec5"]])
    return w_ih, w_hh, b_ih, b_hh


def align_pair(z):
    d, w, lag = z["dry"], z["wet"], int(z["lag"])
    if lag > 0:
        d, w = d[lag:], w[:len(w) - lag]
    elif lag < 0:
        d, w = d[:lag], w[-lag:]
    n = min(len(d), len(w))
    return d[:n], w[:n]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pair", help="pair.npz from amprig.py capture")
    ap.add_argument("--amp-index", type=int, default=0)
    ap.add_argument("--samples", type=int, default=40000)
    ap.add_argument("--skip", type=int, default=20000, help="skip leading silence")
    ap.add_argument("--controls", type=int, default=5)
    args = ap.parse_args()

    z = np.load(args.pair)
    d, w = align_pair(z)
    d = d[args.skip:args.skip + args.samples]
    w = w[args.skip:args.skip + args.samples]
    print(f"{len(d)} samples @ {int(z['rate'])} Hz, amp index {args.amp_index}")
    print(f"dry rms {np.sqrt(np.mean(d**2)):.5f}   wet rms {np.sqrt(np.mean(w**2)):.5f}\n")

    orders = {"g1=r,g2=z": (0, 1, 2), "g1=z,g2=r": (1, 0, 2)}
    rows = []
    for bname, base in WEIGHT_BASE_CANDIDATES.items():
        try:
            W = load_weights(base, args.amp_index)
        except Exception as e:
            print(f"  base {bname}: cannot parse ({e})")
            continue
        for oname, order in orders.items():
            H = gru_forward(d, *W, order)
            r2, nrms = readout_fit(H, d, w)
            rows.append((r2, nrms, bname, oname))
            print(f"  base {bname:10s} order {oname:10s}  R2 {r2:7.4f}  "
                  f"residual {nrms*100:6.2f}% of wet rms", flush=True)

    # controls: same machinery, scrambled weights. Several seeds, because one
    # control sample tells you nothing about the spread of chance.
    W = load_weights(WEIGHT_BASE_CANDIDATES["lmt_anchor"], args.amp_index)
    ctrl = []
    for seed in range(args.controls):
        rng = np.random.default_rng(seed)
        Wc = tuple(rng.permutation(a.ravel()).reshape(a.shape) for a in W)
        H = gru_forward(d, *Wc, (0, 1, 2))
        ctrl.append(readout_fit(H, d, w)[0])
    r2c = float(np.mean(ctrl))
    print(f"\n  {'CONTROL (scrambled, n=%d)' % args.controls:32s}  "
          f"R2 {r2c:7.4f}  (max {max(ctrl):.4f}, spread {np.std(ctrl):.4f})")

    if rows:
        rows.sort(reverse=True)
        best = rows[0]
        margin = best[0] - max(ctrl)
        print(f"\nbest: base {best[2]}, {best[3]}  (R2 {best[0]:.4f})")
        print(f"margin over control: {margin:+.4f}")
        if margin < 0.05:
            print("INCONCLUSIVE - no candidate beats scrambled weights by a "
                  "useful margin. The readout has enough freedom to fit noise, "
                  "so this does not identify the layout.")
        elif len(rows) > 1 and best[0] - rows[1][0] < 0.02:
            print("AMBIGUOUS - top two candidates are within 0.02 R2 of each "
                  "other; this run does not separate them.")


if __name__ == "__main__":
    main()
