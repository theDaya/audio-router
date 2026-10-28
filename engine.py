"""Audio routing engine: capture each source, mix the sources routed to each output, play.

Built on PortAudio (PyAudioWPatch) in WASAPI shared mode. Every device runs as its own callback stream at the
device's own sample rate. Between each source and each output it feeds sits a Pipe, which converts the sample rate and
absorbs the clock drift between the two devices: they never run at exactly the same rate, so without it the buffer
would slowly overflow or run dry.

PortAudio only sees the devices present when it starts, so the engine watches the Windows device list and restarts
PortAudio when it changes (a Bluetooth headset connecting, a jack plugged in).
"""
import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np
import pyaudiowpatch as pa

import procloop

CH = 2                      # everything is mixed in stereo
BLOCK_S = 0.01              # 10 ms callbacks
TARGET_S = 0.04             # latency each pipe aims to hold
MAX_S = 0.3                 # beyond this something stalled; jump back to TARGET_S rather than lag behind
MAX_CORR = 0.003            # most a pipe speeds up or slows down to hold TARGET_S (0.3%, inaudible)
CHECK_S = 2.0               # how often devices are checked for coming and going
LIMIT = 0.97                # the limiter keeps output peaks below this
RELEASE = 0.03              # limiter recovery per block: about 0.3 s back to full gain

PC_AUDIO = "pc"             # source id of "PC audio": what Config.pc_device says to capture
ALL_APPS = "*all apps*"     # Config.pc_device for every app except this one (process loopback, no driver needed)
LOOPBACK = " [Loopback]"    # PyAudioWPatch's suffix for loopback devices


class Pipe:
    """Audio from one source on its way to one output.

    The source pushes at its rate and clock; the output pulls at its own. The pipe reads with a fractional step of
    src_rate / out_rate, nudged by up to MAX_CORR so the fill stays near TARGET_S, and interpolates (Catmull-Rom).
    When the source goes quiet (a loopback stops sending when nothing plays) the output gets silence, and the pipe
    primes again before resuming.
    """

    def __init__(self, src_rate, out_rate):
        self.lock = threading.Lock()
        self.ratio = src_rate / out_rate
        self.target = TARGET_S * src_rate
        self.max = MAX_S * src_rate
        self.buf = np.zeros((0, CH), np.float32)
        self.pos = 1.0              # read position in buf; frame pos-1 is kept as history for the interpolation
        self.primed = False
        self.fill = self.target     # smoothed fill, so jitter in when each side wakes up does not steer

    def push(self, block):
        with self.lock:
            if not len(self.buf):
                block = np.concatenate([block[:1], block])   # history frame for the first interpolation
            self.buf = np.concatenate([self.buf, block])
            if len(self.buf) - self.pos > self.max:
                cut = int(len(self.buf) - self.target) - 1
                self.buf = self.buf[cut:]
                self.pos, self.fill = 1.0, self.target

    def pull(self, n):
        """n frames at the output rate, or None while priming or starved."""
        with self.lock:
            have = len(self.buf) - self.pos
            if not self.primed:
                if have < self.target:
                    return None
                self.primed = True
            self.fill += 0.02 * (have - self.fill)
            corr = np.clip((self.fill - self.target) / self.target * 0.01, -MAX_CORR, MAX_CORR)
            step = self.ratio * (1 + corr)
            need = self.pos + n * step + 2
            if need > len(self.buf):
                self.buf, self.pos, self.primed = self.buf[-1:], 1.0, False
                return None
            x = self.pos + step * np.arange(n)
            self.pos += step * n
            i = x.astype(np.int64)
            t = (x - i)[:, None].astype(np.float32)
            p0, p1, p2, p3 = self.buf[i - 1], self.buf[i], self.buf[i + 1], self.buf[i + 2]
            drop = int(self.pos) - 1
            if drop > 0:
                self.buf, self.pos = self.buf[drop:], self.pos - drop
        return p1 + 0.5 * t * (p2 - p0 + t * (2 * p0 - 5 * p1 + 4 * p2 - p3 + t * (3 * (p1 - p2) + p3 - p0)))


class Limiter:
    """Lets an output be boosted past 100% without crackling: peaks that would clip are turned down smoothly
    (ramped across the block) and the gain recovers over about 0.3 s."""

    def __init__(self):
        self.gain = 1.0
        self.active = False

    def __call__(self, mix):
        peak = float(np.abs(mix).max()) if len(mix) else 0.0
        want = min(1.0, LIMIT / peak) if peak > 0 else 1.0
        new = want if want < self.gain else self.gain + (1.0 - self.gain) * RELEASE
        new = min(new, want)
        mix *= np.linspace(self.gain, new, len(mix), dtype=np.float32)[:, None]
        np.clip(mix, -LIMIT, LIMIT, out=mix)      # the ramp can trail a sudden peak by a few ms
        self.gain, self.active = new, new < 0.999
        return mix


class Config:
    """What the window edits. outputs: list of dicts {device, volume, routes: set of source ids}, volume 1.0 = 100%.
    gains: source id -> gain. Devices are identified by name; source ids are input-device names, plus PC_AUDIO.
    pc_device: ALL_APPS, or a playback device whose loopback is PC audio. default_out: Windows' default output,
    filled in by the engine (it matters for ALL_APPS: that is where apps already play)."""

    def __init__(self, pc_device=None, outputs=None, gains=None, default_out=None):
        self.pc_device = pc_device
        self.outputs = outputs or [{"device": None, "volume": 1.0, "routes": set()} for _ in range(2)]
        self.gains = gains or {}
        self.default_out = default_out

    def native_device(self):
        """The output where PC audio already plays without us."""
        return self.default_out if self.pc_device == ALL_APPS else self.pc_device

    def live_routes(self):
        """(output index, source id) pairs that can actually play. PC audio is never copied onto the device it already
        plays on: with a device loopback the copy would be captured again and echo forever, and with all apps it would
        just double up."""
        for i, o in enumerate(self.outputs):
            if not o["device"]:
                continue
            for s in o["routes"]:
                if s == PC_AUDIO and (not self.pc_device or self.native_device() == o["device"]):
                    continue
                yield i, s


class _WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]


class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD)]


def windows_devices():
    """The names (cut to 31 characters) of the playback and capture devices Windows has right now, and which output is
    the default. Unlike PortAudio's list this is live, so it tells us when to rescan."""
    wm = ctypes.windll.winmm
    names = []
    for count, caps, get in ((wm.waveOutGetNumDevs, _WAVEOUTCAPSW, wm.waveOutGetDevCapsW),
                             (wm.waveInGetNumDevs, _WAVEINCAPSW, wm.waveInGetDevCapsW)):
        for i in range(count()):
            c = caps()
            if get(i, ctypes.byref(c), ctypes.sizeof(c)) == 0:
                names.append(c.szPname)
    preferred, flags = wintypes.DWORD(), wintypes.DWORD()
    wm.waveOutMessage(wintypes.HANDLE(0xFFFFFFFF), 0x2015,                    # WAVE_MAPPER, DRVM_MAPPER_PREFERRED_GET
                      ctypes.byref(preferred), ctypes.byref(flags))
    return tuple(sorted(names)), preferred.value


class _Stream:
    """One open device. Subclasses supply the callback."""

    def __init__(self, engine, key, info, inp):
        self.engine, self.key, self.info = engine, key, info
        self.rate = int(info["defaultSampleRate"])
        self.channels = info["maxInputChannels"] if inp else info["maxOutputChannels"]
        self.n = int(self.rate * BLOCK_S)
        self.last = time.monotonic()
        self.stream = engine.pa.open(format=pa.paFloat32, channels=self.channels, rate=self.rate,
                                     frames_per_buffer=self.n, stream_callback=self._cb,
                                     **({"input": True, "input_device_index": info["index"]} if inp else
                                        {"output": True, "output_device_index": info["index"]}))

    def _cb(self, data, frames, time_info, status):
        self.last = time.monotonic()
        try:
            return self.callback(data, frames), pa.paContinue
        except Exception:
            return None if isinstance(self, _Source) else bytes(frames * self.channels * 4), pa.paContinue

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        self.engine.levels.pop(self.key, None)


class _Source(_Stream):
    def __init__(self, engine, sid, info):
        super().__init__(engine, ("src", sid), info, True)
        self.sid = sid

    def callback(self, data, frames):
        d = np.frombuffer(data, np.float32).reshape(frames, self.channels)
        d = np.repeat(d[:, :1], CH, 1) if self.channels < CH else d[:, :CH].copy()
        d *= self.engine.config.gains.get(self.sid, 1.0)
        self.engine.levels[self.key] = float(np.abs(d).max()) if frames else 0.0
        for pipe in self.engine.pipes_from(self.sid):
            pipe.push(d)
        return None


class _AppsSource:
    """PC audio from every app but this one (procloop.py), shaped like the PortAudio sources."""

    def __init__(self, engine, sid):
        self.engine, self.sid, self.key = engine, sid, ("src", sid)
        self.rate = procloop.RATE
        self.capture = procloop.ProcessLoopback(self.callback)
        self.stream = self      # the watcher asks stream.is_active()

    def callback(self, d):
        d *= self.engine.config.gains.get(self.sid, 1.0)
        self.engine.levels[self.key] = float(np.abs(d).max()) if len(d) else 0.0
        for pipe in self.engine.pipes_from(self.sid):
            pipe.push(d)

    def is_active(self):
        return self.capture.is_active()

    def start_stream(self):
        pass

    def close(self):
        self.capture.close()
        self.engine.levels.pop(self.key, None)


class _Output(_Stream):
    def __init__(self, engine, index, info):
        super().__init__(engine, ("out", index), info, False)
        self.index = index
        self.limiter = Limiter()

    def callback(self, data, frames):
        mix = np.zeros((frames, CH), np.float32)
        for pipe in self.engine.pipes_to(self.index):
            b = pipe.pull(frames)
            if b is not None:
                mix += b
        mix *= self.engine.config.outputs[self.index]["volume"]
        mix = self.limiter(mix)
        self.engine.levels[self.key] = float(np.abs(mix).max())
        if self.channels == CH:
            return mix.tobytes()
        out = np.zeros((frames, self.channels), np.float32)   # surround device: stereo on front left/right,
        out[:, :min(CH, self.channels)] = mix[:, :self.channels]  # mono device: the left channel
        return out.tobytes()


class Engine:
    """Keeps one stream per device in use. apply() opens and closes streams to match a Config; it is cheap to call on
    every change in the window. A watcher thread reopens devices that come back and restarts PortAudio when Windows'
    device list changes."""

    def __init__(self):
        self.lock = threading.RLock()
        self.pa = pa.PyAudio()
        self.config = Config()
        self.pipes = {}          # (output index, source id) -> Pipe
        self.sources = {}        # source id -> _Source
        self.outputs = {}        # output index -> _Output
        self.levels = {}         # ("src", id) / ("out", index) -> peak of the last block, for the meters
        self.missing = {}        # ("src", id) / ("out", index) -> why that device could not be opened
        self.running = False
        self.devices_changed = None   # set by the window: called (from the watcher thread) after a rescan
        self._seen = windows_devices()
        self._closed = False
        self.tones = 0           # test tones playing; a rescan waits for them
        self.tones_done = threading.Condition()
        threading.Thread(target=self._watch, daemon=True).start()

    # ---------- devices ----------

    def _wasapi(self):
        """WASAPI devices, minus Bluetooth Hands-Free ones: opening those switches the headset into call mode, which
        would disturb a call another app (Zoom) has running on it. Streams are always shared mode, never exclusive,
        so nothing here can take a device away from another app."""
        api = self.pa.get_host_api_info_by_type(pa.paWASAPI)["index"]
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d["hostApi"] == api and "hands-free" not in d["name"].lower():
                yield d

    def list_devices(self):
        """(output names, input names, default output name). Loopbacks are not listed: they are reached through
        PC audio."""
        with self.lock:
            devs = list(self._wasapi())
            api = self.pa.get_host_api_info_by_type(pa.paWASAPI)
            default = self.pa.get_device_info_by_index(api["defaultOutputDevice"])["name"] \
                if api["defaultOutputDevice"] >= 0 else None
        outs = [d["name"] for d in devs if d["maxOutputChannels"] > 0]
        ins = [d["name"] for d in devs if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice")]
        return outs, ins, default

    def _find(self, name, kind):
        for d in self._wasapi():
            if kind == "out" and d["maxOutputChannels"] > 0 and d["name"] == name:
                return d
            if kind == "loop" and d.get("isLoopbackDevice") and d["name"] == name + LOOPBACK:
                return d
            if kind == "in" and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice") and d["name"] == name:
                return d
        raise LookupError("not connected")

    def _rescan(self):
        """Restart PortAudio so it sees the current devices, then reopen everything."""
        with self.tones_done:   # a test tone writes outside the lock; never pull PortAudio out from under it
            self.tones_done.wait_for(lambda: self.tones == 0, timeout=3)
        with self.lock:
            for w in list(self.sources.values()) + list(self.outputs.values()):
                w.close()
            self.sources, self.outputs = {}, {}
            self.pa.terminate()
            self.pa = pa.PyAudio()
            self.apply(self.config)
        if self.devices_changed:
            self.devices_changed()

    def _watch(self):
        while not self._closed:
            time.sleep(CHECK_S)
            try:
                now = windows_devices()
                if now != self._seen:
                    self._seen = now
                    time.sleep(1.0)   # let Windows finish setting up a device that just connected
                    self._rescan()
                    continue
                with self.lock:
                    stale = [w for w in list(self.sources.values()) + list(self.outputs.values())
                             if not w.stream.is_active() or
                             (isinstance(w, _Output) and time.monotonic() - w.last > CHECK_S)]
                    if stale or self.missing:   # a device died, or one we want may be back: try again
                        for w in stale:
                            w.close()
                            kind, k = w.key
                            (self.sources if kind == "src" else self.outputs).pop(k, None)
                        if stale:
                            self._rescan()
                        else:
                            self.apply(self.config)
            except Exception:
                pass

    # ---------- routing ----------

    def pipes_from(self, sid):
        return [p for (i, s), p in list(self.pipes.items()) if s == sid]

    def pipes_to(self, index):
        return [p for (i, s), p in list(self.pipes.items()) if i == index]

    def apply(self, config):
        with self.lock:
            self.config = config
            config.default_out = self.list_devices()[2]
            routes = set(config.live_routes()) if self.running else set()
            missing = {}

            want_src = {}
            for _, s in routes:
                want_src[s] = (config.pc_device, "loop") if s == PC_AUDIO else (s, "in")
            for sid, w in list(self.sources.items()):
                if sid not in want_src or w.want != want_src[sid]:
                    w.close()
                    del self.sources[sid]
            for sid, want in want_src.items():
                if sid not in self.sources:
                    try:
                        w = _AppsSource(self, sid) if want[0] == ALL_APPS else _Source(self, sid, self._find(*want))
                        w.want = want
                        w.stream.start_stream()
                        self.sources[sid] = w
                    except Exception as e:
                        missing[("src", sid)] = str(e) or type(e).__name__

            want_out = {i: config.outputs[i]["device"] for i, _ in routes}
            for i, w in list(self.outputs.items()):
                if want_out.get(i) != w.info["name"]:
                    w.close()
                    del self.outputs[i]
            for i, dev in want_out.items():
                if i not in self.outputs:
                    try:
                        w = _Output(self, i, self._find(dev, "out"))
                        self.outputs[i] = w
                    except Exception as e:
                        missing[("out", i)] = str(e) or type(e).__name__

            # pipes need both ends' rates; keep existing ones so a change elsewhere does not drop their audio
            pipes = {}
            for i, s in routes:
                if i in self.outputs and s in self.sources:
                    old = self.pipes.get((i, s))
                    ratio = self.sources[s].rate / self.outputs[i].rate
                    pipes[(i, s)] = old if old and old.ratio == ratio else \
                        Pipe(self.sources[s].rate, self.outputs[i].rate)
            self.pipes = pipes
            for w in self.outputs.values():
                if not w.stream.is_active():
                    w.stream.start_stream()
            self.missing = missing

    def start(self):
        self.running = True
        self.apply(self.config)

    def stop(self):
        self.running = False
        self.apply(self.config)
        self.levels.clear()

    def close(self):
        self._closed = True
        self.stop()
        with self.lock:
            self.pa.terminate()

    def limiting(self, index):
        w = self.outputs.get(index)
        return bool(w and w.limiter.active)

    def test_tone(self, name):
        """One second of 440 Hz on a playback device, alongside whatever the engine is playing there."""
        def play():
            try:
                with self.lock:
                    info = self._find(name, "out")
                    rate, ch = int(info["defaultSampleRate"]), info["maxOutputChannels"]
                    s = self.pa.open(format=pa.paFloat32, channels=ch, rate=rate, output=True,
                                     output_device_index=info["index"])
                t = np.arange(rate) / rate
                tone = 0.2 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, np.minimum(t, 1 - t) * 20)
                s.write(np.repeat(tone[:, None], ch, 1).astype(np.float32).tobytes())
                with self.lock:
                    s.stop_stream()
                    s.close()
            except Exception:
                pass
            finally:
                with self.tones_done:
                    self.tones -= 1
                    self.tones_done.notify_all()
        with self.tones_done:
            self.tones += 1
        threading.Thread(target=play, daemon=True).start()
