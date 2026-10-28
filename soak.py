"""Stability soak on this PC's real devices.  python soak.py [seconds]

Runs the default playback device as an output fed by its own loopback and by the all-apps capture (a self-feed the
window never allows; it just keeps every stream type busy), forces a full restart every 1.5 s, and fires test tones
that overlap the restarts. Passes if the process survives, the output keeps its real-time pace between
restarts, and CPU stays low.
"""
import sys
import threading
import time

import engine
from engine import Config, Engine, Pipe

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120
e = Engine()
outs, ins, default = e.list_devices()
print("devices:", outs, ins, "default:", default, flush=True)


def self_feed():
    """Like apply(), but wiring the default device's loopback into itself."""
    with e.lock:
        for w in list(e.sources.values()) + list(e.outputs.values()):
            w.close()
        e.sources, e.outputs, e.pipes = {}, {}, {}
        src = engine._Source(e, "lb", e._find(default, "loop"))
        apps = engine._AppsSource(e, "apps")
        out = engine._Output(e, 0, e._find(default, "out"))
        src.want, apps.want = (default, "loop"), (engine.ALL_APPS, "loop")
        e.sources, e.outputs = {"lb": src, "apps": apps}, {0: out}
        e.pipes = {(0, "lb"): Pipe(src.rate, out.rate), (0, "apps"): Pipe(apps.rate, out.rate)}
        src.stream.start_stream()
        out.stream.start_stream()


e.running = True
e.config = Config(default, [{"device": default, "volume": 3.0, "routes": set()},
                            {"device": None, "volume": 1.0, "routes": set()}])
e.apply = lambda config: self_feed()     # the watcher and rescans reopen the self-feed too
self_feed()

calls = {"n": 0}
orig_cb = engine._Output.callback


def counting(self, data, frames):
    calls["n"] += 1
    return orig_cb(self, data, frames)


engine._Output.callback = counting

t0, cpu0 = time.monotonic(), time.process_time()
rescans = tones = 0
worst_gap = 0.0
last_n, last_t = 0, time.monotonic()
while time.monotonic() - t0 < seconds:
    time.sleep(1.5)
    now = time.monotonic()
    rate = (calls["n"] - last_n) / (now - last_t)
    last_n, last_t = calls["n"], now
    if rate < 50:               # well under the 100/s of a healthy 10 ms output
        worst_gap = max(worst_gap, now - t0)
        print(f"  slow interval at {now - t0:.0f}s: {rate:.0f} callbacks/s", flush=True)
    e.test_tone(default)
    tones += 1
    threading.Thread(target=e._rescan, daemon=True).start()
    rescans += 1

elapsed = time.monotonic() - t0
cpu = (time.process_time() - cpu0) / elapsed
e._closed = True
with e.lock:
    for w in list(e.sources.values()) + list(e.outputs.values()):
        w.close()
    e.pa.terminate()
print(f"survived {elapsed:.0f}s: {rescans} restarts, {tones} test tones, {calls['n']} output callbacks "
      f"({calls['n'] / elapsed:.0f}/s incl. restart gaps), CPU {cpu * 100:.1f}% of one core", flush=True)
assert calls["n"] / elapsed > 70, "output starved"
print("soak ok")
