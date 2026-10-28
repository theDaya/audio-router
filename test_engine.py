"""Offline checks for the engine: no audio devices needed.  python test_engine.py"""
import numpy as np

from engine import ALL_APPS, BLOCK_S, CH, PC_AUDIO, TARGET_S, Config, Limiter, Pipe


def simulate(src_rate, out_rate, ppm, seconds=300, jitter=0.0, freq=1000.0):
    """Push a sine from a source whose clock is `ppm` off, pull at the output clock with random wake-up jitter.
    Returns (min fill, max fill) in seconds after the first second, underruns, and the error against an ideal sine
    at the output rate (as a fraction of the amplitude)."""
    rng = np.random.default_rng(1)
    pipe = Pipe(src_rate, out_rate)
    ns, no = int(src_rate * BLOCK_S), int(out_rate * BLOCK_S)
    src_period = ns / (src_rate * (1 + ppm / 1e6))
    phase, t_src, fills, underruns, out = 0, 0.0, [], 0, []
    for k in range(int(seconds / BLOCK_S)):
        t_out = k * BLOCK_S + rng.uniform(0, jitter)
        while t_src <= t_out:
            n = np.arange(phase, phase + ns)
            pipe.push(np.repeat((0.5 * np.sin(2 * np.pi * freq * n / src_rate))[:, None], CH, 1).astype(np.float32))
            phase += ns
            t_src += src_period
        was = pipe.primed
        b = pipe.pull(no)
        if b is None:
            underruns += was
            continue
        if k > 1 / BLOCK_S:
            fills.append((len(pipe.buf) - pipe.pos) / src_rate)
            out.append(b[:, 0])
    y = np.concatenate(out)
    # a click, dropout or bad interpolation shows up as a jump in curvature; a clean sine's curvature is bounded by
    # amplitude * (2 pi f / rate)^2 (the pipe's 0.3% speed nudges barely change it)
    clean = 0.5 * (2 * np.pi * freq / out_rate) ** 2
    worst = float(np.abs(np.diff(y, 2)).max()) / clean
    return min(fills), max(fills), underruns, worst


def test_pipe():
    for src, out in ((48000, 48000), (44100, 48000), (48000, 44100), (16000, 48000)):
        for ppm in (-1000, 0, 1000):
            lo, hi, under, err = simulate(src, out, ppm, jitter=0.008)
            print(f"  {src}->{out} {ppm:+5} ppm: fill {lo * 1000:.0f}-{hi * 1000:.0f} ms, underruns {under}, "
                  f"worst curvature {err:.3f}x a clean sine")
            assert under == 0, "underrun"
            assert 0.015 < lo and hi < 0.08, "latency wandered"
            assert err < 1.05, "click, dropout or distortion"


def test_limiter():
    lim = Limiter()
    rng = np.random.default_rng(2)
    peaks = []
    for k in range(400):    # 4 s of loud noise boosted 4x, then 4 s of quiet
        amp = 0.6 if k < 200 else 0.05
        block = (rng.uniform(-amp, amp, (480, CH)) * 4).astype(np.float32)
        out = lim(block)
        peaks.append(float(np.abs(out).max()))
    assert max(peaks) <= 0.97 + 1e-6, "limiter let a peak through"
    quiet = peaks[-50:]
    assert min(quiet) > 0.05 * 4 * 0.85, "gain did not recover after the loud part"
    print(f"  loud peaks held at {max(peaks[:200]):.3f}; quiet part back to {np.mean(quiet) / 0.2:.0%} of boosted level")


def test_routes():
    c = Config(pc_device="bt", outputs=[{"device": "jack", "volume": 1, "routes": {"mic", PC_AUDIO}},
                                        {"device": "bt", "volume": 1, "routes": {PC_AUDIO}},
                                        {"device": None, "volume": 1, "routes": {"mic"}}])
    assert set(c.live_routes()) == {(0, "mic"), (0, PC_AUDIO)}, "PC audio must not loop back into its own device"
    c.pc_device = None
    assert set(c.live_routes()) == {(0, "mic")}
    c.pc_device, c.default_out = ALL_APPS, "jack"      # all apps: they already play on the Windows default
    assert set(c.live_routes()) == {(0, "mic"), (1, PC_AUDIO)}


if __name__ == "__main__":
    test_routes()
    print("routes ok")
    test_limiter()
    print("limiter ok")
    test_pipe()
    print("pipe ok")
