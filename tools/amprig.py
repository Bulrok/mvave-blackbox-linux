#!/usr/bin/env python3
"""Measurement rig: use the pedal as a reference implementation of its own amp.

Signal path
    PipeWire -> Scarlett analog out -> pedal input -> pedal DSP
             -> pedal USB capture (44.1 kHz) -> PipeWire

The Scarlett runs at 48 kHz and the pedal at 44.1 kHz, so what the pedal's ADC
actually sees is NOT bit-known from the file we play. Fitting a model against
an assumed input would be fitting to a signal that never existed. So the pedal
is used as its own reference:

    pass 1  all blocks bypassed  -> captures the DRY signal as the pedal hears it
    pass 2  AMP block only       -> captures the WET signal

Both passes play the identical file down the identical analog path, so the
pair (dry, wet) is a valid input/output dataset for the amp model regardless of
what the analog path did. Alignment between the two is recovered by
cross-correlation rather than assumed.

Commands
    discover    find which Scarlett output channel feeds the pedal
    capture     record a (dry, wet) pair for a given amp index
    verify      play a short tone and report levels, no pedal changes
"""
import argparse
import json
import os
import subprocess
import struct
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blackbox import Pedal, WORKING, BLOCKS, BLOCK_ENABLE  # noqa: E402

# PipeWire node names embed each unit's USB serial, so they are not hardcoded.
# Resolution order: --source/--sink/--sink-channels flags, then the environment
# (AMPRIG_SOURCE, AMPRIG_SINK, AMPRIG_SINK_CHANNELS), then amprig.local.json
# next to this script (untracked; same keys in lower case: "source", "sink",
# "sink_channels"). There is deliberately no auto-detection: the pedal's
# generic "Jieli Technology USB Composite Device" name is shared by other
# Jieli-based gadgets (e.g. wireless mic receivers), so guessing can record
# from the wrong device.
LOCAL_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "amprig.local.json")


def _local_config():
    try:
        with open(LOCAL_CONFIG) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


_CFG = _local_config()
PEDAL_SRC = os.environ.get("AMPRIG_SOURCE") or _CFG.get("source")
SCARLETT_SINK = os.environ.get("AMPRIG_SINK") or _CFG.get("sink")
SCARLETT_CH = int(os.environ.get("AMPRIG_SINK_CHANNELS") or _CFG.get("sink_channels", 8))
SCARLETT_RATE = 48000
PEDAL_RATE = 44100


# ------------------------------------------------------------------ wav io

def write_wav(path, data, rate, channels):
    """data: float array shape (frames, channels) in -1..1, written as 24-bit."""
    q = np.clip(np.round(data * 8388607.0), -8388608, 8388607).astype(np.int32)
    body = bytearray()
    for frame in q.reshape(-1, channels):
        for v in frame:
            body += int(v).to_bytes(3, "little", signed=True)
    ba = 3 * channels
    hdr = (b"RIFF" + (36 + len(body)).to_bytes(4, "little") + b"WAVEfmt " +
           (16).to_bytes(4, "little") + (1).to_bytes(2, "little") +
           channels.to_bytes(2, "little") + rate.to_bytes(4, "little") +
           (rate * ba).to_bytes(4, "little") + ba.to_bytes(2, "little") +
           (24).to_bytes(2, "little") + b"data" + len(body).to_bytes(4, "little"))
    with open(path, "wb") as fh:
        fh.write(hdr + bytes(body))


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
    if not fmt or data is None:
        sys.exit(f"{path}: not a usable WAV")
    _, ch, rate, _, _, bits = fmt
    if bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64) / 8388608.0
    elif bits == 32:
        v = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
    elif bits == 16:
        v = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    else:
        sys.exit(f"{path}: unsupported depth {bits}")
    return v.reshape(-1, ch), rate


# ------------------------------------------------------------------ transport

def play_and_record(play_path, rec_path, seconds, rec_rate=PEDAL_RATE):
    """Start capture, play, stop. Returns recorded (frames, ch) array."""
    rec = subprocess.Popen(
        ["pw-record", "--target", PEDAL_SRC, "--rate", str(rec_rate),
         "--channels", "2", "--format", "s24", rec_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)                      # let the capture stream settle
    subprocess.run(["pw-play", "--target", SCARLETT_SINK, play_path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    rec.terminate()
    rec.wait()
    if not os.path.exists(rec_path) or os.path.getsize(rec_path) < 1000:
        sys.exit("capture produced no audio - is the pedal source correct?")
    return read_wav(rec_path)


def tone(seconds, freq, rate, amp):
    t = np.arange(int(seconds * rate)) / rate
    env = np.minimum(1.0, np.minimum(t * 50, (seconds - t) * 50))   # de-click
    return amp * np.sin(2 * np.pi * freq * t) * env


# ------------------------------------------------------------------ commands

def cmd_discover(args):
    """Play a tone on each Scarlett channel; see which one reaches the pedal."""
    scratch = args.scratch
    os.makedirs(scratch, exist_ok=True)
    print(f"probing {SCARLETT_CH} Scarlett output channels at amplitude {args.amp}\n")
    results = []
    for ch in range(SCARLETT_CH):
        buf = np.zeros((int(args.seconds * SCARLETT_RATE), SCARLETT_CH))
        buf[:, ch] = tone(args.seconds, args.freq, SCARLETT_RATE, args.amp)
        p = os.path.join(scratch, f"probe_ch{ch}.wav")
        write_wav(p, buf, SCARLETT_RATE, SCARLETT_CH)
        rec, rate = play_and_record(p, os.path.join(scratch, f"rec_ch{ch}.wav"),
                                    args.seconds)
        mono = rec.mean(axis=1)
        rms = float(np.sqrt(np.mean(mono ** 2)))
        peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
        results.append((ch, rms, peak))
        print(f"  ch {ch}: rms {rms:.6f}  peak {peak:.6f}"
              f"   {'<-- SIGNAL' if rms > 1e-4 else ''}")
    live = [r for r in results if r[1] > 1e-4]
    print()
    if not live:
        print("No channel produced signal. Check the cable, the Scarlett's "
              "headphone level, and that the pedal input is the one connected.")
    else:
        best = max(live, key=lambda r: r[1])
        print(f"Scarlett output channel {best[0]} feeds the pedal "
              f"(rms {best[1]:.6f}, peak {best[2]:.6f}).")
        print(f"Use: --channel {best[0]}")


def set_blocks(p, enabled):
    """enabled: set of block names to turn on; everything else off."""
    for name in BLOCKS:
        p.write(BLOCK_ENABLE[name], bytes([1 if name in enabled else 0]))


def cmd_capture(args):
    os.makedirs(args.outdir, exist_ok=True)
    n = int(args.seconds * SCARLETT_RATE)
    rng = np.random.default_rng(args.seed)
    # broadband excitation: the amp is nonlinear, so a sweep alone under-covers
    # amplitude space. Noise bursts at several levels probe the transfer curve.
    sig = np.zeros(n)
    seg = n // 4
    t = np.arange(seg) / SCARLETT_RATE
    sig[:seg] = np.sin(2 * np.pi * 20 * (1000 / 20) ** (t / t[-1]) * t)   # sweep
    for k, lvl in enumerate([1.0, 0.5, 0.15], start=1):
        sig[k * seg:(k + 1) * seg] = lvl * rng.standard_normal(seg) * 0.3
    sig = np.clip(sig, -1, 1) * args.amp
    buf = np.zeros((n, SCARLETT_CH))
    buf[:, args.channel] = sig
    play = os.path.join(args.outdir, "excitation.wav")
    write_wav(play, buf, SCARLETT_RATE, SCARLETT_CH)

    with Pedal(args.port) as p:
        before = p.read(WORKING, 92)
        if before is None:
            sys.exit("no response from pedal")
        print(f"amp index currently {before[0x0F]}, saving state for restore")

        print("pass 1: all blocks bypassed -> dry")
        set_blocks(p, set())
        dry, rate = play_and_record(play, os.path.join(args.outdir, "dry.wav"),
                                    args.seconds)

        print("pass 2: AMP only -> wet")
        set_blocks(p, {"AMP"})
        if args.amp_level is not None:
            # Backing off the amp's output level lets the input be driven much
            # harder without clipping the wet capture, which is the only way to
            # lift the dry signal clear of the pedal's input noise floor.
            p.write(WORKING + 0x22, int(args.amp_level).to_bytes(2, "little"))
            print(f"  amp level set to {args.amp_level}")
        if args.amp_index is not None:
            p.write(WORKING + 0x0F, bytes([args.amp_index]))
        wet, _ = play_and_record(play, os.path.join(args.outdir, "wet.wav"),
                                 args.seconds)

        print("restoring preset from flash (Program Change)")
        p.program_change(p.current_slot() or 0)

    d = dry.mean(axis=1)
    w = wet.mean(axis=1)

    def report(x, label):
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        # count samples pinned at full scale - a clipped capture is useless for
        # fitting, and the amp block has enough gain to clip easily
        clipped = int(np.sum(np.abs(x) >= 0.9999))
        print(f"{label}: {len(x)} frames  rms {rms:.6f} peak {peak:.6f}"
              f"  clipped {clipped} ({clipped / max(len(x), 1) * 100:.3f}%)")
        return clipped, peak

    print()
    report(d, "dry")
    wclip, wpeak = report(w, "wet")
    if wclip:
        print(f"\n  WARNING: the wet capture is clipping. Lower --amp (try "
              f"{args.amp * 0.6 / max(wpeak, 1e-9):.2f}) or reduce the amp block's "
              f"level, and re-run. Clipped data will not fit.")
    lag = align(d, w)
    print(f"dry/wet lag: {lag} samples ({lag / rate * 1000:.2f} ms)")
    np.savez(os.path.join(args.outdir, "pair.npz"), dry=d, wet=w, rate=rate, lag=lag)
    print(f"saved {os.path.join(args.outdir, 'pair.npz')}")


def align(a, b, max_lag=20000):
    """Lag (samples) that best aligns b to a, by cross-correlation."""
    n = min(len(a), len(b))
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    fa = np.fft.rfft(a, 2 * n)
    fb = np.fft.rfft(b, 2 * n)
    cc = np.fft.irfft(fa * np.conj(fb), 2 * n)
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag]])
    return int(np.argmax(cc) - max_lag)


def cmd_verify(args):
    """Measure the captured level.

    By default this bypasses every block first. Without that the measurement
    is confounded: it reports whatever the pedal's current preset happens to
    be doing to the signal, so two runs are not comparable and the number is
    useless for setting input level. --processed keeps the current preset.
    """
    buf = np.zeros((int(args.seconds * SCARLETT_RATE), SCARLETT_CH))
    buf[:, args.channel] = tone(args.seconds, args.freq, SCARLETT_RATE, args.amp)
    p = os.path.join(args.scratch, "verify.wav")
    os.makedirs(args.scratch, exist_ok=True)
    write_wav(p, buf, SCARLETT_RATE, SCARLETT_CH)

    ped = None
    if not args.processed:
        ped = Pedal(args.port).__enter__()
        set_blocks(ped, set())
        print("all blocks bypassed - measuring the input as the pedal hears it")
    try:
        rec, rate = play_and_record(p, os.path.join(args.scratch, "verify_rec.wav"),
                                    args.seconds)
    finally:
        if ped is not None:
            ped.program_change(ped.current_slot() or 0)   # restore from flash
            ped.__exit__(None, None, None)

    m = rec.mean(axis=1)
    print(f"captured {len(m)} frames @ {rate} Hz")
    if not len(m):
        return
    peak = float(np.max(np.abs(m)))
    clipped = int(np.sum(np.abs(m) >= 0.9999))
    print(f"  rms {np.sqrt(np.mean(m**2)):.6f}  peak {peak:.6f}  clipped {clipped}")
    sp = np.abs(np.fft.rfft(m * np.hanning(len(m))))
    f = np.fft.rfftfreq(len(m), 1 / rate)
    print(f"  dominant frequency {f[np.argmax(sp)]:.1f} Hz (played {args.freq} Hz)")
    if args.amp > 0:
        if peak > 0.9:
            print("  too hot - back the Scarlett off")
        elif peak < 0.25:
            print("  quiet - room to raise the Scarlett (aim for peak 0.5-0.7)")
        else:
            print("  good input level")


AMP_PARAM_BASE = WORKING + 0x20      # five u16 params seen at 0x20..0x28


def cmd_params(args):
    """Identify the amp block's parameters by measuring their effect.

    Sweeping each parameter low/high and watching output level separates a
    post-gain 'level' control (scales output, little change in distortion)
    from a pre-gain 'gain' control (raises level AND distortion). Knowing
    which is level lets you back it off and drive the input harder, which is
    the only way to improve SNR without clipping the wet capture.
    """
    os.makedirs(args.scratch, exist_ok=True)
    buf = np.zeros((int(args.seconds * SCARLETT_RATE), SCARLETT_CH))
    buf[:, args.channel] = tone(args.seconds, args.freq, SCARLETT_RATE, args.amp)
    play = os.path.join(args.scratch, "param_tone.wav")
    write_wav(play, buf, SCARLETT_RATE, SCARLETT_CH)

    def measure(tag):
        rec, rate = play_and_record(play, os.path.join(args.scratch, f"p_{tag}.wav"),
                                    args.seconds)
        m = rec.mean(axis=1)
        if not len(m):
            return 0.0, 0.0
        rms = float(np.sqrt(np.mean(m ** 2)))
        sp = np.abs(np.fft.rfft(m * np.hanning(len(m))))
        f = np.fft.rfftfreq(len(m), 1 / rate)
        k = int(np.argmin(np.abs(f - args.freq)))
        fund = sp[max(k - 3, 0):k + 4].sum()
        rest = sp.sum() - fund
        return rms, float(rest / max(fund, 1e-12))     # crude THD proxy

    with Pedal(args.port) as p:
        orig = p.read(WORKING, 92)
        if orig is None:
            sys.exit("no response from pedal")
        set_blocks(p, {"AMP"})
        print(f"{'param':>5} {'addr':>12}  {'lo rms':>9} {'lo thd':>7}  "
              f"{'hi rms':>9} {'hi thd':>7}   interpretation")
        for i in range(5):
            addr = AMP_PARAM_BASE + 2 * i
            was = int.from_bytes(orig[0x20 + 2 * i:0x22 + 2 * i], "little")
            p.write(addr, (args.lo).to_bytes(2, "little"))
            lo_rms, lo_thd = measure(f"{i}_lo")
            p.write(addr, (args.hi).to_bytes(2, "little"))
            hi_rms, hi_thd = measure(f"{i}_hi")
            p.write(addr, was.to_bytes(2, "little"))
            ratio = hi_rms / max(lo_rms, 1e-9)
            dthd = hi_thd - lo_thd
            if ratio > 1.5 and dthd < 0.05:
                note = "LEVEL-like (scales output, distortion flat)"
            elif ratio > 1.5:
                note = "GAIN-like (louder and more distorted)"
            elif dthd > 0.05:
                note = "tone/drive (distortion changes, level flat)"
            else:
                note = "little effect on a steady tone"
            print(f"{i:>5} 0x{addr:08X}  {lo_rms:9.5f} {lo_thd:7.3f}  "
                  f"{hi_rms:9.5f} {hi_thd:7.3f}   {note}")
        print("\nrestoring preset from flash")
        p.program_change(p.current_slot() or 0)


def cmd_latency(args):
    """Benchmark the pedal as an audio interface.

    Absolute round-trip cannot be measured honestly with separate pw-play and
    pw-record processes: their start jitter was +-700 samples in practice,
    which swamps the quantity of interest. So the DSP latency is measured
    INSIDE ONE continuous recording -- two identical bursts played with a gap,
    with the block config toggled during the gap. Both bursts share one time
    base, so start jitter cancels exactly.
    """
    os.makedirs(args.scratch, exist_ok=True)
    rate = SCARLETT_RATE
    burst_n = int(0.5 * rate)
    gap_n = int(args.gap * rate)
    t = np.arange(burst_n) / rate
    # log chirp: sharp correlation peak, broadband enough to survive the amp
    burst = 0.9 * np.sin(2 * np.pi * 200 * (4000 / 200) ** (t / t[-1]) * t)
    burst *= np.minimum(1.0, np.minimum(t * 200, (t[-1] - t) * 200))
    sig = np.concatenate([np.zeros(int(0.3 * rate)), burst,
                          np.zeros(gap_n), burst, np.zeros(int(0.3 * rate))])
    buf = np.zeros((len(sig), SCARLETT_CH))
    buf[:, args.channel] = sig * args.amp
    play = os.path.join(args.scratch, "lat.wav")
    write_wav(play, buf, rate, SCARLETT_CH)

    a_off = int(0.3 * rate) * PEDAL_RATE // rate          # expected, in capture samples
    b_off = (int(0.3 * rate) + burst_n + gap_n) * PEDAL_RATE // rate

    with Pedal(args.port) as p:
        set_blocks(p, set())                               # burst A: bypassed
        rec_path = os.path.join(args.scratch, "lat_rec.wav")
        recp = subprocess.Popen(
            ["pw-record", "--target", PEDAL_SRC, "--rate", str(PEDAL_RATE),
             "--channels", "2", "--format", "s24", rec_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.7)
        playp = subprocess.Popen(["pw-play", "--target", SCARLETT_SINK, play],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # toggle to AMP while the gap is playing
        time.sleep(0.3 + 0.5 + args.gap * 0.5)
        set_blocks(p, {"AMP"})
        playp.wait()
        time.sleep(0.4)
        recp.terminate(); recp.wait()
        p.program_change(p.current_slot() or 0)

    rec, rrate = read_wav(rec_path)
    m = rec.mean(axis=1)
    ref = np.interp(np.linspace(0, len(burst) - 1, burst_n * PEDAL_RATE // rate),
                    np.arange(len(burst)), burst)

    # Find BOTH bursts as the two strongest correlation peaks, rather than
    # assuming where they land: pw-play's startup delay is large and variable,
    # so absolute positions are not predictable. Only their separation is.
    c = np.abs(np.correlate(m, ref, mode="valid"))
    p1 = int(np.argmax(c))
    guard = int(0.4 * PEDAL_RATE)
    c2 = c.copy()
    c2[max(p1 - guard, 0):p1 + guard] = 0
    p2 = int(np.argmax(c2))
    posA, posB = sorted((p1, p2))
    strength = c[p1] / (np.median(c) or 1)
    print(f"capture: {len(m)} frames @ {rrate} Hz")
    print(f"  burst A (bypassed) at sample {posA}")
    print(f"  burst B (AMP on)   at sample {posB}")
    print(f"  peak/median correlation ratio {strength:.1f} (higher = clearer detection)")
    expected = b_off - a_off
    delta = (posB - posA) - expected
    print(f"  expected separation {expected} samples, measured {posB - posA}")
    print(f"\n  AMP block DSP latency: {delta:+d} samples "
          f"({delta / PEDAL_RATE * 1000:+.3f} ms) relative to bypass")
    if abs(delta) > PEDAL_RATE // 10:
        print("  IMPLAUSIBLE (>100 ms) - burst detection probably failed; "
              "treat as invalid rather than as a real measurement")
    print("  (both bursts share one recording, so play/record start jitter cancels)")


def main():
    global PEDAL_SRC, SCARLETT_SINK, SCARLETT_CH
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-p", "--port")
    ap.add_argument("--scratch", default="/tmp/amprig")
    ap.add_argument("--source", help="PipeWire node of the pedal's USB capture "
                    "(default: $AMPRIG_SOURCE or amprig.local.json)")
    ap.add_argument("--sink", help="PipeWire node of the interface feeding the "
                    "pedal's input (default: $AMPRIG_SINK or amprig.local.json)")
    ap.add_argument("--sink-channels", type=int,
                    help=f"output channel count of --sink (default {SCARLETT_CH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="find the Scarlett channel feeding the pedal")
    s.add_argument("--seconds", type=float, default=1.0)
    s.add_argument("--freq", type=float, default=440.0)
    s.add_argument("--amp", type=float, default=0.2)
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("verify", help="play a tone and report captured level")
    s.add_argument("--channel", type=int, required=True)
    s.add_argument("--seconds", type=float, default=2.0)
    s.add_argument("--freq", type=float, default=440.0)
    s.add_argument("--amp", type=float, default=0.2)
    s.add_argument("--processed", action="store_true",
                   help="keep the current preset instead of bypassing blocks")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("params", help="identify the amp block parameters")
    s.add_argument("--channel", type=int, required=True)
    s.add_argument("--seconds", type=float, default=1.5)
    s.add_argument("--freq", type=float, default=220.0)
    s.add_argument("--amp", type=float, default=0.12)
    s.add_argument("--lo", type=int, default=15)
    s.add_argument("--hi", type=int, default=90)
    s.set_defaults(func=cmd_params)

    s = sub.add_parser("latency", help="benchmark the pedal as an audio interface")
    s.add_argument("--channel", type=int, required=True)
    s.add_argument("--gap", type=float, default=2.0)
    s.add_argument("--amp", type=float, default=0.5)
    s.set_defaults(func=cmd_latency)

    s = sub.add_parser("capture", help="record a (dry, wet) pair")
    s.add_argument("--channel", type=int, required=True)
    s.add_argument("--seconds", type=float, default=8.0)
    s.add_argument("--amp", type=float, default=0.2)
    s.add_argument("--amp-index", type=int)
    s.add_argument("--amp-level", type=int,
                   help="amp block output level 0-100; lower it to allow a hotter input")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--outdir", default="/tmp/amprig/capture")
    s.set_defaults(func=cmd_capture)

    args = ap.parse_args()
    PEDAL_SRC = args.source or PEDAL_SRC
    SCARLETT_SINK = args.sink or SCARLETT_SINK
    SCARLETT_CH = args.sink_channels or SCARLETT_CH
    if not PEDAL_SRC:
        sys.exit("no pedal capture source configured - pass --source "
                 "(see `pactl list short sources`) or set AMPRIG_SOURCE")
    if not SCARLETT_SINK:
        sys.exit("no output sink configured - pass --sink "
                 "(see `pactl list short sinks`) or set AMPRIG_SINK")
    args.func(args)


if __name__ == "__main__":
    main()
