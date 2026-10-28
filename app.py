"""Audio Router: play different mixes of your sources (PC audio, microphones) on two outputs at once.

The window is a matrix: one row per source, one column per output, a tick where a source should play.
Settings are saved in %APPDATA%\\AudioRouter\\config.json.
"""
import json
import math
import os
import sys
import tkinter as tk
import winreg
from tkinter import ttk

import procloop
from engine import ALL_APPS, PC_AUDIO, Config, Engine

CONFIG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AudioRouter", "config.json")
NONE = "(none)"
MAX_BOOST = 400            # output volume slider goes to 400% (+12 dB); the limiter keeps it from crackling
HELP = ("PC audio is everything your apps play. Windows already plays it on the default output, so it can't be "
        "ticked there (use the Windows volume for that one), and the router boosts and mixes the other output. "
        "Apps set to a specific speaker in their own settings are copied as well.\n"
        "Bluetooth Hands-Free devices are hidden: opening one switches the headset to call mode, which would disturb "
        "a call in Zoom or another app.\n"
        "Volume above 100% boosts that output; a limiter turns loud peaks down instead of letting them crackle.")


def label(name):
    return name


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOGIN_COMMAND = f'"{sys.executable}" --minimized'


def launch_at_login():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "AudioRouter")[0] == LOGIN_COMMAND
    except OSError:
        return False


def set_launch_at_login(on):
    """Adds or removes this .exe in the current user's sign-in programs (Task Manager > Startup apps lists it)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "AudioRouter", 0, winreg.REG_SZ, LOGIN_COMMAND)
            else:
                winreg.DeleteValue(k, "AudioRouter")
    except OSError:
        pass


def missing(name):
    return f"(not connected) {name}"


def meter_value(peak):
    """Peak 0..1 to a 0..100 bar on a 60 dB scale."""
    return 0 if peak <= 1e-6 else max(0.0, 100 + 100 / 60 * 20 * math.log10(peak))


def volume_text(pct):
    if pct < 0.5:
        return "muted"
    return f"{pct:.0f}%  ({20 * math.log10(pct / 100):+.1f} dB)"


class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        self.rescanned = False
        self.engine.devices_changed = lambda: setattr(self, "rescanned", True)   # from the engine's thread
        self.saved = self.load()
        self.cfg = None
        self.all_apps = procloop.supported()
        root.title("Audio Router")
        root.minsize(800, 300)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        top = ttk.Frame(root, padding=(12, 10, 12, 0))
        top.pack(fill="x")
        ttk.Label(top, text="PC audio comes from:").pack(side="left")
        self.pc_var = tk.StringVar()
        self.pc_box = ttk.Combobox(top, textvariable=self.pc_var, state="readonly", width=48)
        self.pc_box.pack(side="left", padx=6)
        self.pc_box.bind("<<ComboboxSelected>>", lambda e: self.changed())
        ttk.Button(top, text="Refresh devices", command=self.rebuild).pack(side="right")
        self.default_label = ttk.Label(root, foreground="gray", padding=(12, 4, 12, 0))
        self.default_label.pack(fill="x")

        self.grid = ttk.Frame(root, padding=12)
        self.grid.pack(fill="both", expand=True)

        bottom = ttk.Frame(root, padding=(12, 0, 12, 10))
        bottom.pack(fill="x")
        self.run_btn = ttk.Button(bottom, text="Start", command=self.toggle, width=12)
        self.run_btn.pack(side="left")
        self.auto_var = tk.BooleanVar(value=self.saved.get("autostart", False))
        ttk.Checkbutton(bottom, text="Start routing when the app opens", variable=self.auto_var,
                        command=self.save).pack(side="left", padx=12)
        if getattr(sys, "frozen", False):   # only the packaged .exe can sensibly be launched at sign-in
            self.login_var = tk.BooleanVar(value=launch_at_login())
            ttk.Checkbutton(bottom, text="Launch at Windows sign-in", variable=self.login_var,
                            command=lambda: set_launch_at_login(self.login_var.get())).pack(side="left")
        self.status = ttk.Label(bottom, text="Stopped", foreground="gray")
        self.status.pack(side="right")
        ttk.Label(root, text=HELP, foreground="gray", wraplength=780, justify="left",
                  padding=(12, 0, 12, 12)).pack(fill="x")

        self.rebuild()
        if self.auto_var.get():
            self.toggle()
        self.tick()

    # ---------- settings ----------

    def load(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self):
        c = self.engine.config
        data = {"autostart": self.auto_var.get(), "pc_device": c.pc_device, "gains": c.gains,
                "outputs": [{"device": o["device"], "volume": o["volume"], "routes": sorted(o["routes"])}
                            for o in c.outputs]}
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError:
            pass

    def initial_config(self, default_out):
        s = self.saved
        outputs = [{"device": o.get("device"), "volume": o.get("volume", 1.0), "routes": set(o.get("routes", []))}
                   for o in s.get("outputs", [])][:2]
        outputs += [{"device": None, "volume": 1.0, "routes": set()} for _ in range(2 - len(outputs))]
        pc = s.get("pc_device", ALL_APPS if self.all_apps else default_out)
        return Config(pc, outputs, dict(s.get("gains", {})))

    # ---------- window ----------

    def rebuild(self):
        """(Re)list devices and draw the matrix. Settings for devices that are not connected are kept, so a Bluetooth
        headset that is off right now is remembered and picked up again when it connects."""
        outs, ins, default_out = self.engine.list_devices()
        self.default_out = default_out
        cfg = self.cfg = self.cfg or self.initial_config(default_out)
        self.default_label.configure(text=f"Windows default output: {default_out or '(none)'}, where apps already play")

        def choices(current):
            c = [(NONE, None)] + [(label(n), n) for n in outs]
            if current and current not in outs and current != ALL_APPS:
                c.append((missing(current), current))
            return c

        self.pc_choices = [(NONE, None)]
        if self.all_apps:
            self.pc_choices.append(("All apps (except this one)", ALL_APPS))
        self.pc_choices += [(f"Only what plays on: {n}", i) for n, i in choices(cfg.pc_device)[1:]]
        if cfg.pc_device == ALL_APPS and not self.all_apps:
            cfg.pc_device = default_out
        self.pc_box["values"] = [n for n, _ in self.pc_choices]
        self.pc_var.set(next(n for n, i in self.pc_choices if i == cfg.pc_device))

        for w in self.grid.winfo_children():
            w.destroy()
        g = self.grid
        g.columnconfigure(0, weight=1)
        bold = ("Segoe UI", 9, "bold")
        ttk.Label(g, text="Source", font=bold).grid(row=0, column=0, sticky="sw")
        ttk.Label(g, text="Level", font=bold).grid(row=0, column=1, sticky="s")
        ttk.Label(g, text="Gain", font=bold).grid(row=0, column=2, sticky="s")

        self.out_widgets = []
        for k, o in enumerate(cfg.outputs):
            col = 3 + k
            g.columnconfigure(col, weight=1, minsize=250)
            box = ttk.Frame(g, padding=(10, 0, 0, 6))
            box.grid(row=0, column=col, sticky="nsew")
            ttk.Label(box, text=f"Output {k + 1}", font=bold).pack(anchor="w")
            ch = choices(o["device"])
            var = tk.StringVar(value=next(n for n, i in ch if i == o["device"]))
            cb = ttk.Combobox(box, textvariable=var, values=[n for n, _ in ch], state="readonly", width=36)
            cb.pack(fill="x")
            cb.bind("<<ComboboxSelected>>", lambda e: self.changed())
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(4, 0))
            ttk.Label(row, text="Vol").pack(side="left")
            vol = tk.DoubleVar(value=o["volume"] * 100)
            vol_text = ttk.Label(box, text=volume_text(vol.get()), foreground="gray")

            def moved(v, vol=vol, vol_text=vol_text):
                vol_text.configure(text=volume_text(float(v)))
                self.changed(quiet=True)
            ttk.Scale(row, from_=0, to=MAX_BOOST, variable=vol, command=moved).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(row, text="100%", width=5,
                       command=lambda vol=vol, m=moved: (vol.set(100), m(100))).pack(side="left")
            ttk.Button(row, text="Test", width=5, command=lambda k=k: self.test(k)).pack(side="left", padx=(4, 0))
            vol_text.pack(anchor="w")
            meter = ttk.Progressbar(box, maximum=100, length=200)
            meter.pack(fill="x", pady=(4, 0))
            err = ttk.Label(box, text="", foreground="#b00", wraplength=240)
            err.pack(anchor="w")
            self.out_widgets.append({"choices": ch, "var": var, "vol": vol, "meter": meter, "err": err,
                                     "ticks": {}})

        sources = [(PC_AUDIO, "PC audio")] + [(n, label(n)) for n in ins]
        routed = set().union(*(o["routes"] for o in cfg.outputs))
        sources += [(sid, missing(sid)) for sid in sorted(routed) if sid != PC_AUDIO and sid not in ins]
        self.src_widgets = {}
        for r, (sid, name) in enumerate(sources, start=1):
            cell = ttk.Frame(g)
            cell.grid(row=r, column=0, sticky="w", pady=4)
            ttk.Label(cell, text=name, wraplength=240).pack(anchor="w")
            err = ttk.Label(cell, text="", foreground="#b00")
            err.pack(anchor="w")
            meter = ttk.Progressbar(g, maximum=100, length=90)
            meter.grid(row=r, column=1, padx=6)
            gain = tk.DoubleVar(value=cfg.gains.get(sid, 1.0) * 100)
            ttk.Scale(g, from_=0, to=300, variable=gain, length=90,
                      command=lambda v: self.changed(quiet=True)).grid(row=r, column=2, padx=6)
            self.src_widgets[sid] = {"meter": meter, "gain": gain, "err": err}
            for k, o in enumerate(cfg.outputs):
                var = tk.BooleanVar(value=sid in o["routes"])
                chk = ttk.Checkbutton(g, variable=var, command=self.changed)
                chk.grid(row=r, column=3 + k)
                self.out_widgets[k]["ticks"][sid] = (var, chk)
        self.changed()

    def read(self):
        """The window's state as a Config."""
        pc = dict(self.pc_choices).get(self.pc_var.get())
        outputs = []
        for w in self.out_widgets:
            outputs.append({"device": dict(w["choices"]).get(w["var"].get()), "volume": w["vol"].get() / 100,
                            "routes": {sid for sid, (v, _) in w["ticks"].items() if v.get()}})
        gains = {sid: w["gain"].get() / 100 for sid, w in self.src_widgets.items()}
        return Config(pc, outputs, gains, self.default_out)

    def changed(self, quiet=False):
        cfg = self.cfg = self.read()
        if quiet:   # a slider moved: the streams read volume and gain on every block, nothing to reopen
            self.engine.config.gains = cfg.gains
            for o, n in zip(self.engine.config.outputs, cfg.outputs):
                o["volume"] = n["volume"]
        else:
            for k, w in enumerate(self.out_widgets):
                var, chk = w["ticks"][PC_AUDIO]
                native = cfg.pc_device and cfg.outputs[k]["device"] == cfg.native_device()
                chk.configure(state="disabled" if native else "normal",
                              text="plays here natively" if native else "")
            self.engine.apply(cfg)
        self.save()

    def test(self, k):
        dev = dict(self.out_widgets[k]["choices"]).get(self.out_widgets[k]["var"].get())
        if dev:
            self.engine.test_tone(dev)

    def toggle(self):
        if self.engine.running:
            self.engine.stop()
            self.run_btn.configure(text="Start")
        else:
            self.engine.start()
            self.run_btn.configure(text="Stop")

    def tick(self):
        if self.rescanned:          # the engine restarted after Windows' device list changed
            self.rescanned = False
            self.rebuild()
        lv, errs = self.engine.levels, dict(self.engine.missing)
        for sid, w in self.src_widgets.items():
            w["meter"]["value"] = meter_value(lv.get(("src", sid), 0))
            w["err"].configure(text="not available, retrying" if ("src", sid) in errs else "")
        for k, w in enumerate(self.out_widgets):
            w["meter"]["value"] = meter_value(lv.get(("out", k), 0))
            if ("out", k) in errs:
                w["err"].configure(text="Can't open this device, retrying", foreground="#b00")
            elif self.engine.limiting(k):
                w["err"].configure(text="Limiting loud peaks", foreground="#b60")
            else:
                w["err"].configure(text="")
        if not self.engine.running:
            self.status.configure(text="Stopped", foreground="gray")
        elif errs:
            self.status.configure(text="Running, some devices unavailable", foreground="#b60")
        else:
            n = len(self.engine.pipes)
            self.status.configure(text=f"Running: {n} route{'s' * (n != 1)}", foreground="#070")
        self.root.after(60, self.tick)

    def quit(self):
        self.save()
        self.engine.close()
        self.root.destroy()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)   # sharp text on scaled displays; must come before the window
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    if "--minimized" in sys.argv:   # launched at sign-in: route quietly in the taskbar
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
