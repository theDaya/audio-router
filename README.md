# Audio Router

Plays different mixes of your sources (PC audio, microphones) on two outputs at once. For example, the 3.5mm headphones
get the USB mic plus PC audio, and the Bluetooth headset gets PC audio only, boosted.

`AudioRouter.exe` (from the repo's Releases page) is self-contained: copy it to the PC and run it. No Python and no
drivers are needed. Settings are saved in `%APPDATA%\AudioRouter\config.json`.

## Setting it up for headphones + Bluetooth

PC audio is captured with Windows' process loopback, which records everything your apps play except the router
itself (Windows 10 2004+ or Windows 11). Windows still plays your apps on its default output as usual, so:

1. In Windows Sound settings, make the **3.5mm headphones** the default output.
2. In Audio Router:
   - **PC audio comes from:** All apps (except this one)
   - **Output 1:** Headphones (Realtek). Tick your *USB mic*. PC audio is greyed out ("plays here natively"),
     because Windows already plays it there.
   - **Output 2:** Headphones (your Bluetooth headset). Tick *PC audio*, and raise *Vol* above 100% to boost it.
3. Press **Start**. Tick *Start routing when the app opens* and *Launch at Windows sign-in* if the PC should always
   do this.

The default output gets PC audio from Windows itself, so the router can't boost or remove it there; use the Windows
volume for that output. If you ever want both outputs fully under the router's control, make the Windows default a
device you don't listen to (for example a monitor without speakers) and tick PC audio on both.

## Zoom and other apps

- Every device is opened in Windows' shared mode, never exclusive, so other apps keep full use of the same devices.
  The router never changes Windows' default device or any volume. If it crashed, Windows would just close its
  streams.
- Bluetooth **Hands-Free** devices are hidden. Opening one switches the headset into call mode, which would disturb a
  Zoom call on that headset.
- Zoom's meeting audio is part of "all apps", so it reaches the Bluetooth output too.
- The router's own output (for example your mic on the headphones) is not captured into PC audio. However, if Zoom
  is sharing computer sound, Zoom may pick up what the router plays on the default output, mic included. Worth a quick
  check before relying on it.
- If an app is set to play on a specific speaker in its own settings, it is captured too, so on that speaker it plays
  twice. Leave apps on "Same as System" / "Default".

## Notes

- Latency: about 40–60 ms through the router, plus the Bluetooth headset's own delay (usually 150–250 ms), so the
  outputs won't be in sync. That's fine for two listeners, but noticeable if you hear your own mic over Bluetooth.
- Boost goes up to 400% (+12 dB). A limiter turns loud peaks down instead of letting them crackle, and the window
  shows "Limiting loud peaks" while it does.
- Devices can come and go (headset off, jack pulled, default output changed). The router notices, shows "retrying",
  and picks them up again when they return.
- On Windows older than 10 2004, "All apps" isn't offered. Pick "Only what plays on: <device>" instead, which records
  one output's loopback.

## Development

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt pyinstaller
.venv\Scripts\python test_engine.py        # offline: routing, limiter, rate conversion + clock drift
.venv\Scripts\python soak.py 120           # real devices: restarts, test tones, CPU
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name AudioRouter app.py
```
