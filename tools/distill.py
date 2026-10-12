#!/usr/bin/env python3
"""Distil a NAM capture into the Blackbox's GRU architecture.

Teacher   NAM WaveNet (.nam), run with the official neural-amp-modeler code
          rather than a reimplementation, so weight-ordering mistakes cannot
          silently produce a wrong target.
Student   GRU(input=1, hidden=16) + linear readout -- the architecture the
          pedal actually runs (see PROTOCOL.md).

PyTorch's GRU gate order is (r, z, n), which is exactly the order established
for the pedal (gate1=reset, gate2=update, gate3=candidate), so student weights
map onto the AM4 layout without permutation.

Rates: NAM runs at 48 kHz, the pedal at 44.1 kHz. The teacher is run at its
native rate and the pair is then resampled to 44.1 kHz, so the student learns
the amp at the rate it will actually run.
"""
import argparse
import copy
import glob
import json
import os
import struct
import sys
import types

import numpy as np

for _m in ("tkinter", "tkinter.filedialog", "tkinter.messagebox"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import torch
import torch.nn as nn
from scipy.signal import resample_poly

NAM_RATE = 48000
PEDAL_RATE = 44100
HIDDEN = 16


def load_nam(path):
    d = json.load(open(path))
    cfg = copy.deepcopy(d)
    for la in cfg["config"]["layers"]:
        if "head_size" in la:      # v0.5.0 schema -> current schema
            la["head"] = {"out_channels": la.pop("head_size"),
                          "kernel_size": 1, "bias": la.pop("head_bias")}
    from nam.models import init_from_nam
    net = init_from_nam(cfg)
    net.eval()
    n = sum(p.numel() for p in net.parameters())
    assert n + 1 == len(d["weights"]), (
        f"parameter count {n}+1 != {len(d['weights'])} weights; architecture mismatch")
    return net


def read_wav(path):
    raw = open(path, "rb").read()
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", raw[pos + 8:pos + 24])
        elif cid == b"data":
            data = raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    _, ch, rate, _, _, bits = fmt
    if bits == 16:
        v = np.frombuffer(data, "<i2").astype(np.float64) / 32768.0
    elif bits == 24:
        b = np.frombuffer(data, np.uint8).reshape(-1, 3).astype(np.int32)
        u = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(u & 0x800000, u - (1 << 24), u).astype(np.float64) / 8388608.0
    else:
        v = np.frombuffer(data, "<i4").astype(np.float64) / 2147483648.0
    return (v.reshape(-1, ch).mean(1) if ch > 1 else v), rate


@torch.no_grad()
def run_teacher(net, x, chunk=200000, overlap=8192):
    """Run NAM over a long signal in chunks, discarding warm-up per chunk."""
    out = np.empty_like(x)
    i = 0
    while i < len(x):
        s = max(i - overlap, 0)
        seg = torch.tensor(x[s:i + chunk], dtype=torch.float32)
        y = net(seg).numpy()
        out[i:i + chunk] = y[i - s: i - s + min(chunk, len(x) - i)]
        i += chunk
    return out


class Student(nn.Module):
    """What the pedal runs: GRU(1,16) then a linear readout of the hidden state.

    The readout also sees the input (a skip path); the pedal's own readout
    location is not yet identified, so this mirrors the 18-parameter form that
    was validated against the hardware.
    """

    def __init__(self, hidden=HIDDEN):
        super().__init__()
        self.gru = nn.GRU(1, hidden, batch_first=True)
        self.out = nn.Linear(hidden + 1, 1)

    def forward(self, x, h=None):
        y, h = self.gru(x, h)
        return self.out(torch.cat([y, x], dim=-1)), h


@torch.no_grad()
def run_long(model, x, chunk=65536):
    """Run the student over a long signal in chunks, carrying hidden state.

    cuDNN's fused GRU refuses sequences of a few hundred thousand steps, so
    the validation signal is streamed rather than passed as one sequence.
    Carrying h across chunks keeps it mathematically identical.
    """
    outs, h = [], None
    for i in range(0, len(x), chunk):
        seg = x[i:i + chunk].unsqueeze(0).unsqueeze(-1).contiguous()
        y, h = model(seg, h)
        outs.append(y.squeeze())
    return torch.cat(outs)


def esr(pred, targ):
    """Error-to-signal ratio, the standard amp-modelling metric."""
    return ((pred - targ) ** 2).sum() / (((targ - targ.mean()) ** 2).sum() + 1e-12)


def prefilter(z):
    """First-order pre-emphasis; weights the high end that ears care about."""
    return z[..., 1:, :] - 0.85 * z[..., :-1, :]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("nam")
    ap.add_argument("--di", default=os.path.expanduser("~/Downloads/di"))
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--seq", type=int, default=4096)
    ap.add_argument("--warmup", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=5e-3)
    ap.add_argument("--out", default="/tmp/distilled.npz")
    args = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # cuDNN's fused GRU rejects these shapes (CUDNN_STATUS_NOT_SUPPORTED) on
    # long sequences. The model is 16 hidden units, so the native kernel is
    # plenty fast and is numerically what we want anyway.
    torch.backends.cudnn.enabled = False
    print(f"device: {dev} (cuDNN RNN disabled)")

    print(f"teacher: {os.path.basename(args.nam)}")
    net = load_nam(args.nam)

    xs = []
    for f in sorted(glob.glob(os.path.join(args.di, "*.wav"))):
        v, r = read_wav(f)
        assert r == NAM_RATE, f"{f} is {r} Hz, expected {NAM_RATE}"
        xs.append(v)
    x48 = np.concatenate(xs).astype(np.float64)
    print(f"DI: {len(x48)/NAM_RATE:.1f}s @ {NAM_RATE} Hz from {len(xs)} clips")

    print("running teacher...")
    y48 = run_teacher(net, x48.astype(np.float32)).astype(np.float64)

    # 48000 -> 44100 is 147/160
    x = resample_poly(x48, 147, 160)
    y = resample_poly(y48, 147, 160)
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    print(f"resampled to {PEDAL_RATE} Hz: {n} samples ({n/PEDAL_RATE:.1f}s)")
    print(f"  input rms {np.sqrt((x**2).mean()):.4f}   teacher rms {np.sqrt((y**2).mean()):.4f}")

    split = int(n * 0.85)
    xt = torch.tensor(x[:split], dtype=torch.float32, device=dev)
    yt = torch.tensor(y[:split], dtype=torch.float32, device=dev)
    xv = torch.tensor(x[split:], dtype=torch.float32, device=dev)
    yv = torch.tensor(y[split:], dtype=torch.float32, device=dev)

    model = Student().to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, args.steps)
    g = torch.Generator(device="cpu").manual_seed(0)

    best = float("inf")
    best_state = None
    for step in range(1, args.steps + 1):
        idx = torch.randint(0, len(xt) - args.seq - 1, (args.batch,), generator=g)
        bx = torch.stack([xt[i:i + args.seq] for i in idx]).unsqueeze(-1).contiguous()
        by = torch.stack([yt[i:i + args.seq] for i in idx]).unsqueeze(-1).contiguous()
        pred, _ = model(bx)
        loss = esr(prefilter(pred[:, args.warmup:]), prefilter(by[:, args.warmup:]))
        opt.zero_grad(set_to_none=True)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        if step % 250 == 0 or step == 1:
            model.eval()
            pv = run_long(model, xv)
            e = float(esr(pv[args.warmup:], yv[args.warmup:]))
            model.train()
            if e < best:
                best, best_state = e, copy.deepcopy(model.state_dict())
            print(f"  step {step:5d}  train ESR {float(loss):.5f}   val ESR {e:.5f}"
                  + ("  *" if e == best else ""), flush=True)

    model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        pv = run_long(model, xv)[args.warmup:]
        yv2 = yv[args.warmup:]
        e = float(esr(pv, yv2))
        r2 = 1 - float(((pv - yv2) ** 2).sum() / ((yv2 - yv2.mean()) ** 2).sum())
    print(f"\nbest held-out ESR {e:.5f}  ({e*100:.2f}%)   R2 {r2:.4f}")

    sd = {k: v.cpu().numpy() for k, v in model.state_dict().items()}
    np.savez(args.out, teacher=os.path.basename(args.nam), esr=e, r2=r2, **sd)
    print(f"saved {args.out}")
    print("  weight_ih_l0 shape", sd["gru.weight_ih_l0"].shape,
          " weight_hh_l0", sd["gru.weight_hh_l0"].shape)


if __name__ == "__main__":
    main()
