# IDÅSEN Desk Control

Windows/Linux/macOS desktop app that connects to an [IKEA IDÅSEN](https://www.ikea.com/) sit/stand desk over Bluetooth Low Energy — written in Python with tkinter and [bleak](https://github.com/hbldh/bleak).

Move the desk to saved positions, watch the live height, and let a configurable sit/stand timer remind you when to switch — with Windows toast notifications, a system-tray icon, and a timer that pauses automatically while your screen is locked.

## Features

**Desk control**

- Connect via automatic discovery or MAC address; optional auto-connect on app start and auto-reconnect after connection loss
- Live height display (cm), manual up/down while holding the button, stop at any time
- Saved positions as one-click buttons (double-click to move), e.g. "Sit" and "Stand"
- Robust BLE layer: automatic wake-up and retry on `Insufficient Authentication` (ATT 0x05), clear error messages

**Sit/stand timer ("Pomodoro for your desk")**

- Alternates between your sit and stand positions at configurable intervals
- Optional automatic desk movement at each switch
- Optionally varies intervals by ±20 % so switches don't become too predictable
- Warning notification before each switch, with **snooze +5 min** (GUI button, tray menu, or toast button)
- Work-time window (e.g. Mon–Fri 09:00–17:00); outside the window the timer freezes
- **Lock-screen pause (Windows):** while the workstation is locked the countdown freezes and resumes automatically on unlock

**System tray (pystray + Pillow)**

- Minimize to tray (close/minimize button)
- Tray icon color shows connection state: **blue = connected, red = disconnected**
- Hover tooltip with countdown and current height, e.g. `IDÅSEN – Sitz-Phase, Wechsel in 32:10 | 112,3 cm`
- Right-click menu: open, stop desk, move to sit/stand position, timer start/pause/stop, snooze +5 min, quit
- Windows toast notifications with buttons (**+5 min.**, **Jetzt wechseln**) via [windows-toasts](https://github.com/DatGuy/Windows-Toasts)

**Autostart**

- Optional start with the system: Windows (registry `HKCU\…\Run`, launches via `pythonw.exe`), Linux (XDG `.desktop`), macOS (LaunchAgent)

## Requirements

- Python 3.10+
- Windows 10/11 (fully supported), Linux, or macOS
- Bluetooth adapter with BLE support
- An IKEA IDÅSEN desk (Linak controller)

## Installation

```bash
git clone https://github.com/anyx9/idasen-bluetooth-control.git
cd idasen-bluetooth-control
python -m pip install bleak pystray Pillow
```

Optional, for toast notifications with buttons on Windows:

```bash
python -m pip install windows-toasts
```

Run:

```bash
python idasen_gui.py
```

## First-time setup

1. Press the desk's Bluetooth button until the LED blinks (pairing mode) — this is only needed **once** for discovery.
2. Click **Connect** in the app. The desk is found and its MAC address is saved.
3. From now on the app connects directly without pairing mode (and automatically on start, if enabled).

Note: the desk accepts only **one BLE connection at a time** — close/disconnect the IKEA app on your phone before connecting.

## Configuration

Settings are stored in `~/.idasen_gui.json` and managed via the GUI. Defaults:

```json
{
  "mac": "",
  "language": "de",
  "positions": [["Sit", 0.72], ["Stand", 1.10]],
  "timer": {
    "sit": "Sit",
    "stand": "Stand",
    "sit_min": 45.0,
    "stand_min": 15.0,
    "auto_move": true,
    "warn": true,
    "warn_min": 1.0,
    "vary": false,
    "work_enabled": false,
    "work_start": "09:00",
    "work_end": "17:00",
    "work_weekdays": true,
    "pause_on_lock": true
  },
  "auto_reconnect": true,
  "auto_connect": true,
  "minimize_to_tray": true
}
```


| Key                                 | Meaning                                                        |
| ----------------------------------- | -------------------------------------------------------------- |
| `mac`                               | Desk MAC address (filled in on first connection)               |
| `positions`                         | Saved positions: `[name, height in meters]`, range 0.62–1.27 m |
| `timer.sit` / `timer.stand`         | Positions the timer alternates between                         |
| `timer.sit_min` / `timer.stand_min` | Phase durations in minutes                                     |
| `timer.auto_move`                   | Move the desk automatically at each switch                     |
| `timer.warn` / `timer.warn_min`     | Warn notification N minutes before the switch                  |
| `timer.vary`                        | Vary intervals by ±20 %                                        |
| `timer.work_*`                      | Work-time window (freeze timer outside)                        |
| `timer.pause_on_lock`               | Pause timer while the screen is locked (Windows)               |
| `auto_reconnect`                    | Reconnect automatically after connection loss                  |
| `auto_connect`                      | Connect automatically on app start                             |
| `minimize_to_tray`                  | Minimize to the system tray instead of the taskbar             |


## Troubleshooting

- **`Insufficient Authentication` (ATT 0x05):** the app sends the wake-up sequence and retries automatically. If it persists, click **Pair** once to pair the desk (the official IKEA app pairs during first setup too). If that doesn't work, try pairing the chair with Windows first. then connect via MAC.
- **Linux:** pair the desk once with `bluetoothctl pair AA:BB:CC:DD:EE:FF`. For the tray icon, GNOME needs the *AppIndicator and KStatusNotifierItem* extension.
- **macOS:** direct MAC connect isn't available; the app falls back to discovery automatically (pair the desk once).
- **Desk not found:** make sure the IKEA app is closed/disconnected — the desk holds only one BLE connection at a time.

## How it works

The BLE protocol (GATT UUIDs, control commands, height parsing) follows the reference implementation of the [idasen](https://github.com/newAM/idasen) Python package. Heights are read from the height characteristic (`raw/10000 + 0.62` m), movement uses the control and reference-input characteristics.

## License

MIT

## Credits

- [newAM/idasen](https://github.com/newAM/idasen) — BLE protocol reference implementation
- [hbldh/bleak](https://github.com/hbldh/bleak) — cross-platform BLE access
- [moses-palmer/pystray](https://github.com/moses-palmer/pystray) — system tray icon
- [DatGuy/Windows-Toasts](https://github.com/DatGuy/Windows-Toasts) — Windows toast notifications
