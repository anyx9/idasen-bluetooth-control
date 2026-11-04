#!/usr/bin/env python3
"""IDÅSEN Desk Control

Controls the IKEA IDÅSEN desk (Linak controller) over Bluetooth Low
Energy (BLE) with a tkinter GUI:

- Connect (automatic discovery or MAC address); optional auto-connect on
  app start and auto-reconnect on connection loss
- Live height display
- Manual up/down (hold the button) and stop
- Saved positions as buttons (list, move via double-click)
- Timer: automatically alternates between the sit and stand position at
  configurable intervals ("a Pomodoro timer for your desk"); optional
  slightly varying intervals (±20 %), system notification before each
  switch with snooze (+5 min) and a work-time window (e.g. only
  Mon–Fri 09:00–17:00 – outside it the timer is paused)
- System tray: the window can be minimized to the tray (× and minimize),
  right-click menu with stop, the two timer positions and timer stop;
  the tray tooltip shows the timer countdown and the current height;
  the tray icon is blue while connected, red when disconnected
- Lock-screen pause (Windows): the timer freezes while the workstation
  is locked and resumes automatically after unlocking
- Autostart: optionally start the app at system startup (Windows:
  registry, Linux: XDG autostart, macOS: LaunchAgent)
- Multi-language UI (German/English), switchable at runtime in Settings

Requirements:
    python -m pip install bleak pystray Pillow
    (pystray + Pillow only needed for the system tray icon)
    Windows: for buttons in the switch notification additionally:
    python -m pip install windows-toasts

Start:
    python idasen-bluetooth-control.py

Notes:
- Pairing mode (press the desk's Bluetooth button twice until the LED
  blinks) is only needed ONCE to discover the desk. The MAC address is
  saved; after that the app connects directly without pairing mode –
  with a saved MAC also automatically at app start.
- The desk can only hold one BLE connection at a time: close or
  disconnect the IKEA app on your phone before connecting.
- On Linux, pair once if necessary:  bluetoothctl pair AA:BB:CC:DD:EE:FF
- If a command fails with ATT error 0x05 "Insufficient Authentication",
  the app automatically sends the wakeup sequence and retries once.
  Pairing once (button "Pair") fixes this permanently – the official
  app also pairs during first-time setup.
- macOS: direct MAC connection is not possible there; the app falls
  back to discovery automatically (pair the desk once).
- System tray on Linux: requires an AppIndicator status area
  (e.g. the GNOME Shell extension "AppIndicator and
  KStatusNotifierItem").
- The BLE protocol (UUIDs, commands) follows the reference
  implementation of the Python package "idasen" (github.com/newAM/idasen).

Configuration is saved to:  ~/.idasen_gui.json
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import random
import struct
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Missing dependency. Please install:  python -m pip install bleak")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
# BLE protocol (IDÅSEN / Linak DPG)
# ---------------------------------------------------------------------------
UUID_HEIGHT = "99fa0021-338a-1024-8a49-009c0215f78a"  # height + speed (read/notify)
UUID_COMMAND = "99fa0002-338a-1024-8a49-009c0215f78a"  # movement commands (write)
UUID_REF_INPUT = "99fa0031-338a-1024-8a49-009c0215f78a"  # target setpoint (write)
UUID_ADV_SVC = "99fa0001-338a-1024-8a49-009c0215f78a"  # advertised service
UUID_DPG = "99fa0011-338a-1024-8a49-009c0215f78a"  # wakeup (DPG controller)

CMD_UP = bytes([0x47, 0x00])
CMD_DOWN = bytes([0x46, 0x00])
CMD_STOP = bytes([0xFF, 0x00])
CMD_WAKEUP = bytes([0xFE, 0x00])
CMD_REF_STOP = bytes([0x01, 0x80])

MIN_HEIGHT = 0.62  # m – lowest IDÅSEN height
MAX_HEIGHT = 1.27  # m – highest IDÅSEN height
TOLERANCE = 0.005  # m – "target reached" tolerance

CONFIG_PATH = Path.home() / ".idasen_gui.json"

DEFAULT_CONFIG = {
    "mac": "",
    "language": "de",
    "positions": [["Sitzen", 0.72], ["Stehen", 1.10]],
    "timer": {
        "sit": "Sitzen",
        "stand": "Stehen",
        "sit_min": 45.0,
        "stand_min": 15.0,
        "auto_move": True,
        "warn": True,
        "warn_min": 1.0,
        "vary": False,
        "work_enabled": False,
        "work_start": "09:00",
        "work_end": "17:00",
        "work_weekdays": True,
        "pause_on_lock": True,
    },
    "auto_reconnect": True,
    "auto_connect": True,
    "minimize_to_tray": True,
}


def load_config() -> dict:
    cfg = {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["timer"] = {**DEFAULT_CONFIG["timer"], **(cfg.get("timer") or {})}
    if not merged["positions"]:
        merged["positions"] = list(DEFAULT_CONFIG["positions"])
    if merged.get("language") not in LANGUAGES:
        merged["language"] = "de"
    return merged


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Internationalization (UI strings; "de" is the fallback language)
# ---------------------------------------------------------------------------
LANG = "de"
LANGUAGES = {"de": "Deutsch", "en": "English"}

STRINGS = {
    "de": {
        "app_title": "IDÅSEN Schreibtisch-Steuerung",
        "frame_connection": "Verbindung",
        "lbl_mac": "MAC (leer = automatisch suchen):",
        "btn_connect": "Verbinden",
        "btn_disconnect": "Trennen",
        "btn_pair": "Koppeln",
        "chk_reconnect": "Bei Verbindungsverlust neu verbinden",
        "chk_autoconnect": "Beim App-Start verbinden",
        "status_not_connected": "Nicht verbunden",
        "frame_desk": "Aktueller Tisch",
        "btn_up": "▲  Hoch",
        "btn_down": "▼  Runter",
        "btn_stop": "Stopp",
        "frame_positions": "Gespeicherte Positionen",
        "btn_goto": "Anfahren",
        "btn_save_pos": "Position speichern …",
        "btn_delete_pos": "Löschen",
        "lbl_doubleclick": "Doppelklick fährt ebenfalls an.",
        "frame_timer": "Timer (Sitz-/Steh-Wechsel)",
        "lbl_sit_pos": "Sitz-Position:",
        "lbl_stand_pos": "Steh-Position:",
        "lbl_duration": "Dauer (Min):",
        "chk_auto_move": "Tisch beim Wechsel automatisch bewegen (sonst nur Erinnerung)",
        "chk_warn": "Vor dem Wechsel benachrichtigen:",
        "lbl_min_before": "Min. vorher",
        "chk_vary": "Dauer leicht variieren (±20 %)",
        "chk_lock_pause": "Bei gesperrtem Bildschirm pausieren (Windows)",
        "chk_work_hours": "Nur zu Arbeitszeiten:",
        "chk_weekdays": "Mo–Fr",
        "msg_timer_stopped": "Timer gestoppt",
        "btn_start_timer": "Timer starten",
        "btn_pause": "Pause",
        "btn_resume": "Fortsetzen",
        "btn_stop_timer": "Timer stoppen",
        "btn_snooze5": "+5 Min.",
        "btn_switch_now": "Sofort wechseln",
        "frame_settings": "Einstellungen",
        "chk_tray": "In den Systray minimieren (bei × und Minimieren)",
        "chk_autostart": "Automatisch beim Systemstart starten",
        "lbl_language": "Sprache / Language:",
        "status_lang_changed": "Sprache geändert: {lang}",
        "tray_open": "Öffnen",
        "tray_stop_desk": "Stopp (Tisch anhalten)",
        "tray_sit_pos": "Sitz-Position: {name}",
        "tray_stand_pos": "Steh-Position: {name}",
        "tray_timer_start": "Timer starten",
        "tray_timer_pause": "Timer pausieren",
        "tray_timer_resume": "Timer fortsetzen",
        "tray_timer_stop": "Timer stoppen",
        "tray_snooze": "Wechsel +5 Min. verschieben",
        "tray_quit": "Beenden",
        "sit_phase": "Sitz-Phase",
        "stand_phase": "Steh-Phase",
        "switch_in": "Wechsel in {time}",
        "paused_suffix": ", pausiert",
        "countdown_locked": "{phase} ({target}) – Bildschirm gesperrt, pausiert{note}",
        "countdown_outside": "{phase} ({target}) – außerhalb der Arbeitszeit, pausiert{note}",
        "countdown_paused": "{phase} ({target}) – PAUSIERT, {time} übrig{note}",
        "countdown_switch": "{phase} ({target}) – Wechsel in {time}{note}",
        "note_reminder_only": "  (nur Erinnerung)",
        "status_searching": "Suche Schreibtisch …",
        "status_connected": "Verbunden ({addr})",
        "status_disconnected": "Getrennt",
        "status_paired": "Verbunden + gekoppelt",
        "status_lost": "Verbindung getrennt",
        "status_reconnecting": "Neuverbindung …",
        "status_pairing": "Koppelung läuft …",
        "status_autostart_on": "Autostart aktiviert",
        "status_autostart_off": "Autostart deaktiviert",
        "err_status": "⚠ Fehler: {msg}",
        "err_generic": "{msg}",
        "err_not_connected": "Nicht verbunden",
        "err_not_found": (
            "Kein IDÅSEN-Schreibtisch gefunden.\n"
            "Tipp: Bluetooth-Taste am Tisch zweimal drücken, "
            "bis die LED blinkt (Pairing-Modus)."
        ),
        "err_connect_failed": "Verbindung fehlgeschlagen: {exc}",
        "err_auth": (
            "Insufficient Authentication (ATT 0x05): Der Tisch verlangt "
            "eine gekoppelte (verschlüsselte) Verbindung.\n"
            "Bitte einmalig koppeln – dazu: 1) diese App schließen, "
            "2) Tisch in den Pairing-Modus versetzen (Taste 2x drücken, "
            "LED blinkt), 3) über die Bluetooth-Einstellungen des "
            "Betriebssystems koppeln (LED hört auf zu blinken), "
            "4) App neu starten und verbinden."
        ),
        "err_systray": "⚠ Systray nicht verfügbar: {exc}",
        "err_autostart": "Autostart nicht änderbar: {exc}",
        "err_height_range": "Höhe außerhalb des IDÅSEN-Bereichs ({min} – {max})",
        "err_pos_not_found": "Position '{name}' nicht gefunden",
        "err_switch_skipped": "Nicht verbunden – Wechsel übersprungen",
        "err_no_height": "Keine Höhendaten – bitte zuerst verbinden",
        "dlg_select_position": "Bitte zuerst eine Position auswählen.",
        "dlg_save_position_title": "Position speichern",
        "dlg_pos_name": "Name der Position:",
        "dlg_height_cm": "Höhe in cm (z. B. 112):",
        "dlg_confirm_delete": 'Position "{name}" wirklich löschen?',
        "dlg_choose_positions": "Bitte Sitz- und Steh-Position wählen.",
        "dlg_invalid_durations": "Bitte gültige Dauern in Minuten eingeben.",
        "notify_snooze_hint": "  – Snooze: „+5 Min.“ im Tray",
        "notify_switch_title": "Wechsel steht bevor",
        "notify_switch_msg": "Wechsel zu {pos} in {time}",
        "btn_switch_now_toast": "Jetzt wechseln",
    },
    "en": {
        "app_title": "IDÅSEN Desk Control",
        "frame_connection": "Connection",
        "lbl_mac": "MAC (empty = search automatically):",
        "btn_connect": "Connect",
        "btn_disconnect": "Disconnect",
        "btn_pair": "Pair",
        "chk_reconnect": "Reconnect after connection loss",
        "chk_autoconnect": "Connect on app start",
        "status_not_connected": "Not connected",
        "frame_desk": "Current desk",
        "btn_up": "▲  Up",
        "btn_down": "▼  Down",
        "btn_stop": "Stop",
        "frame_positions": "Saved positions",
        "btn_goto": "Move to",
        "btn_save_pos": "Save position…",
        "btn_delete_pos": "Delete",
        "lbl_doubleclick": "Double-click also moves the desk.",
        "frame_timer": "Timer (sit/stand switch)",
        "lbl_sit_pos": "Sit position:",
        "lbl_stand_pos": "Stand position:",
        "lbl_duration": "Duration (min):",
        "chk_auto_move": "Move desk automatically at each switch (otherwise reminder only)",
        "chk_warn": "Notify before switch:",
        "lbl_min_before": "min before",
        "chk_vary": "Vary duration slightly (±20 %)",
        "chk_lock_pause": "Pause while screen is locked (Windows)",
        "chk_work_hours": "Only during work hours:",
        "chk_weekdays": "Mon–Fri",
        "msg_timer_stopped": "Timer stopped",
        "btn_start_timer": "Start timer",
        "btn_pause": "Pause",
        "btn_resume": "Resume",
        "btn_stop_timer": "Stop timer",
        "btn_snooze5": "+5 min",
        "btn_switch_now": "Switch now",
        "frame_settings": "Settings",
        "chk_tray": "Minimize to system tray (on × and minimize)",
        "chk_autostart": "Start automatically at system startup",
        "lbl_language": "Sprache / Language:",
        "status_lang_changed": "Language changed: {lang}",
        "tray_open": "Open",
        "tray_stop_desk": "Stop (stop desk)",
        "tray_sit_pos": "Sit position: {name}",
        "tray_stand_pos": "Stand position: {name}",
        "tray_timer_start": "Start timer",
        "tray_timer_pause": "Pause timer",
        "tray_timer_resume": "Resume timer",
        "tray_timer_stop": "Stop timer",
        "tray_snooze": "Postpone switch by +5 min",
        "tray_quit": "Quit",
        "sit_phase": "Sit phase",
        "stand_phase": "Stand phase",
        "switch_in": "switch in {time}",
        "paused_suffix": ", paused",
        "countdown_locked": "{phase} ({target}) – screen locked, paused{note}",
        "countdown_outside": "{phase} ({target}) – outside work hours, paused{note}",
        "countdown_paused": "{phase} ({target}) – PAUSED, {time} left{note}",
        "countdown_switch": "{phase} ({target}) – switch in {time}{note}",
        "note_reminder_only": "  (reminder only)",
        "status_searching": "Searching for desk…",
        "status_connected": "Connected ({addr})",
        "status_disconnected": "Disconnected",
        "status_paired": "Connected + paired",
        "status_lost": "Connection lost",
        "status_reconnecting": "Reconnecting…",
        "status_pairing": "Pairing…",
        "status_autostart_on": "Autostart enabled",
        "status_autostart_off": "Autostart disabled",
        "err_status": "⚠ Error: {msg}",
        "err_generic": "{msg}",
        "err_not_connected": "Not connected",
        "err_not_found": (
            "No IDÅSEN desk found.\n"
            "Tip: press the desk's Bluetooth button twice until the "
            "LED blinks (pairing mode)."
        ),
        "err_connect_failed": "Connection failed: {exc}",
        "err_auth": (
            "Insufficient Authentication (ATT 0x05): the desk requires a "
            "paired (encrypted) connection.\n"
            "Please pair once – how to: 1) close this app, 2) put the "
            "desk into pairing mode (press the button twice, the LED "
            "blinks), 3) pair via your operating system's Bluetooth "
            "settings (the LED stops blinking), 4) restart the app "
            "and connect."
        ),
        "err_systray": "⚠ System tray unavailable: {exc}",
        "err_autostart": "Could not change autostart: {exc}",
        "err_height_range": "Height outside the IDÅSEN range ({min} – {max})",
        "err_pos_not_found": "Position '{name}' not found",
        "err_switch_skipped": "Not connected – switch skipped",
        "err_no_height": "No height data – please connect first",
        "dlg_select_position": "Please select a position first.",
        "dlg_save_position_title": "Save position",
        "dlg_pos_name": "Position name:",
        "dlg_height_cm": "Height in cm (e.g. 112):",
        "dlg_confirm_delete": 'Really delete position "{name}"?',
        "dlg_choose_positions": "Please choose a sit and a stand position.",
        "dlg_invalid_durations": "Please enter valid durations in minutes.",
        "notify_snooze_hint": "  – snooze: '+5 min' in the tray",
        "notify_switch_title": "Switch coming up",
        "notify_switch_msg": "Switch to {pos} in {time}",
        "btn_switch_now_toast": "Switch now",
    },
}


def tr(key: str, **params) -> str:
    """Translate `key` into the current language (fallback: German)."""
    text = STRINGS.get(LANG, {}).get(key) or STRINGS["de"].get(key) or key
    return text.format(**params) if params else text


def app_name() -> str:
    """Localized application name (tray icon, notifications)."""
    return tr("app_title")


# ---------------------------------------------------------------------------
# Autostart (Windows: registry, Linux: XDG autostart, macOS: LaunchAgent)
# ---------------------------------------------------------------------------
APP_ID = "idasen-gui"


def autostart_enable() -> None:
    script = os.path.abspath(__file__)
    if sys.platform.startswith("win"):
        import winreg  # noqa: PLC0415

        exe = sys.executable
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            exe = str(pythonw)  # pythonw: start without a console window
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, f'"{exe}" "{script}"')
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"    <key>Label</key><string>{APP_ID}</string>\n"
            "    <key>ProgramArguments</key>\n    <array>\n"
            f"        <string>{sys.executable}</string>\n"
            f"        <string>{script}</string>\n"
            "    </array>\n"
            "<key>RunAtLoad</key><true/>\n"
            "</dict>\n</plist>\n",
            encoding="utf-8",
        )
    else:  # Linux (XDG autostart)
        target = Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={app_name()}\n"
            f"Exec={sys.executable} {script}\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )


def autostart_disable() -> None:
    try:
        if sys.platform.startswith("win"):
            import winreg  # noqa: PLC0415

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, APP_ID)
        elif sys.platform == "darwin":
            (Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist").unlink(
                missing_ok=True
            )
        else:
            (Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop").unlink(
                missing_ok=True
            )
    except FileNotFoundError:
        pass


def autostart_is_enabled() -> bool:
    if sys.platform.startswith("win"):
        try:
            import winreg  # noqa: PLC0415

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.QueryValueEx(key, APP_ID)
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist").exists()
    return (Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop").exists()


# ---------------------------------------------------------------------------
# BLE thread with its own asyncio event loop
# ---------------------------------------------------------------------------
class BleThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> asyncio.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.loop.stop())


# ---------------------------------------------------------------------------
# Desk backend (runs in the BLE thread, talks to the GUI via a queue)
# ---------------------------------------------------------------------------
class DeskError(Exception):
    """Exception carrying an i18n translation key plus format parameters,
    so the GUI can render the message in the selected language."""

    def __init__(self, key: str, **params) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


class DeskBackend:
    def __init__(self, ble: BleThread, events: queue.Queue):
        self.ble = ble
        self.events = events
        self.client: BleakClient | None = None
        self.address: str | None = None
        self.connected = False
        self.auto_reconnect = True

        self._move_gen = 0  # generation counter: a new move stops the old one
        self._hold_gen = 0
        self._poll_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

    # --- events to the GUI -------------------------------------------------
    # Status and errors are emitted as translation keys (+ params), not as
    # ready-made strings, so the GUI can localize them.
    def _emit(self, kind: str, *args) -> None:
        self.events.put((kind, args))

    def _report(self, fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is None:
            return
        if isinstance(exc, DeskError):
            self._emit("error", exc.key, exc.params)
        else:
            self._emit("error", "err_generic", {"msg": str(exc)})

    def submit(self, coro) -> asyncio.Future:
        fut = self.ble.submit(coro)
        fut.add_done_callback(self._report)
        return fut

    # --- connection ---------------------------------------------------------
    async def connect(self, address: str | None = None) -> None:
        # 1) Known MAC address: connect directly – the desk does NOT have
        #    to be in pairing mode for this (same as the official app).
        if address:
            for attempt in range(3):
                try:
                    client = BleakClient(address, disconnected_callback=self._on_disconnect)
                    await client.connect()
                    await self._finish_connect(client, address)
                    return
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.3 * (attempt + 1))
            # Direct connection failed -> continue with discovery.

        # 2) Discovery via BLE scan (the desk must be in pairing mode).
        def match(device, adv) -> bool:
            if UUID_ADV_SVC in (adv.service_uuids or []):
                return True
            return bool(address) and device.address.lower() == address.lower()

        self._emit("status", "status_searching", {}, False)
        device = await BleakScanner.find_device_by_filter(match, timeout=20.0)
        if device is None:
            raise DeskError("err_not_found")

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                client = BleakClient(device, disconnected_callback=self._on_disconnect)
                await client.connect()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                await asyncio.sleep(0.3 * (attempt + 1))
        else:
            raise DeskError("err_connect_failed", exc=str(last_exc))

        await self._finish_connect(client, device.address)

    async def _finish_connect(self, client: BleakClient, address: str) -> None:
        self.client = client
        self.address = address
        await self._wakeup()

        if self._poll_task:
            self._poll_task.cancel()
        self._poll_task = asyncio.ensure_future(self._poll_height())

        with contextlib.suppress(Exception):
            await self.client.start_notify(UUID_HEIGHT, self._on_height)
        with contextlib.suppress(Exception):
            h, _ = await self.get_height_and_speed()
            self._emit("height", h, 0.0)

        self.connected = True
        self._emit("status", "status_connected", {"addr": self.address}, True)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        self.connected = False
        self._emit("status", "status_disconnected", {}, False)

    async def pair(self) -> None:
        """Pairs with the desk (bonding). Permanently fixes ATT error 0x05
        'Insufficient Authentication'. Needed once per desk; the official
        app does the same during first-time setup."""
        if not self.client or not self.client.is_connected:
            raise DeskError("err_not_connected")
        await self.client.pair()
        self._emit("status", "status_paired", {}, True)

    def _on_disconnect(self, *_args) -> None:
        if self.connected:
            self.connected = False
            self._emit("status", "status_lost", {}, False)
            if self.auto_reconnect and self.address and not self._reconnect_task:
                self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        try:
            while self.auto_reconnect and self.address and not self.connected:
                self._emit("status", "status_reconnecting", {}, False)
                with contextlib.suppress(Exception):
                    await self.connect(self.address)
                if not self.connected:
                    await asyncio.sleep(5.0)
        finally:
            self._reconnect_task = None

    async def _wakeup(self) -> None:
        """Needed for the Linak DPG controller; the original one does not need it."""
        with contextlib.suppress(Exception):
            await self.client.write_gatt_char(UUID_DPG, b"\x7f\x86\x00")
            await self.client.write_gatt_char(
                UUID_DPG, b"\x7f\x86\x80" + bytes(range(1, 18))
            )
        with contextlib.suppress(Exception):
            await self.client.write_gatt_char(UUID_COMMAND, CMD_WAKEUP)

    # --- height ---------------------------------------------------------------
    @staticmethod
    def _parse_height(raw: bytes) -> tuple[float, float]:
        int_raw, speed_raw = struct.unpack("<Hh", bytes(raw[:4]))
        return int_raw / 10000.0 + MIN_HEIGHT, speed_raw / 10000.0

    def _on_height(self, _char, data: bytearray) -> None:
        with contextlib.suppress(Exception):
            h, s = self._parse_height(data)
            self._emit("height", h, s)

    async def get_height_and_speed(self) -> tuple[float, float]:
        if not self.client or not self.client.is_connected:
            raise DeskError("err_not_connected")
        return self._parse_height(await self.client.read_gatt_char(UUID_HEIGHT))

    async def _poll_height(self) -> None:
        """Polls the height every 2 s in addition to notifications (fallback)."""
        while self.client and self.client.is_connected:
            with contextlib.suppress(Exception):
                h, s = await self.get_height_and_speed()
                self._emit("height", h, s)
            await asyncio.sleep(2.0)

    # --- movement -------------------------------------------------------------
    async def _write(self, uuid: str, data: bytes, response: bool = False) -> None:
        """Write with a retry on 'Insufficient Authentication' (ATT 0x05):
        the wakeup sequence briefly opens a window for write access,
        afterwards the write is retried once. If it still fails, the desk
        has to be paired (bonded) once via the operating system."""
        try:
            await self.client.write_gatt_char(uuid, data, response=response)
        except Exception as exc:
            if "auth" not in str(exc).lower():
                raise
            await self._wakeup()
            try:
                await self.client.write_gatt_char(uuid, data, response=response)
            except Exception as exc2:
                if "auth" not in str(exc2).lower():
                    raise
                raise DeskError("err_auth") from exc2

    async def _stop_writes(self) -> None:
        if self.client and self.client.is_connected:
            with contextlib.suppress(Exception):
                await self._write(UUID_COMMAND, CMD_STOP, response=False)
            with contextlib.suppress(Exception):
                await self._write(UUID_REF_INPUT, CMD_REF_STOP, response=False)

    async def move_to_target(self, target: float) -> None:
        """Moves the desk to the target height (in meters)."""
        if not self.client or not self.client.is_connected:
            raise DeskError("err_not_connected")
        target = min(max(target, MIN_HEIGHT), MAX_HEIGHT)
        self._move_gen += 1
        self._hold_gen += 1  # cancel a running hold
        gen = self._move_gen
        try:
            h, _ = await self.get_height_and_speed()
            if abs(h - target) < TOLERANCE:
                return
            data = struct.pack("<H", int(round((target - MIN_HEIGHT) * 10000)))
            with contextlib.suppress(Exception):
                # wakeup + stop start the reference input for target moves
                await self._write(UUID_COMMAND, CMD_WAKEUP, response=True)
                await self._write(UUID_COMMAND, CMD_STOP, response=True)
            zero_speed = 0
            stall_retries = 0
            deadline = time.monotonic() + 90.0
            while self._move_gen == gen and time.monotonic() < deadline:
                await self._write(UUID_REF_INPUT, data)
                await asyncio.sleep(0.1)
                h, speed = await self.get_height_and_speed()
                self._emit("height", h, speed)
                if speed == 0:
                    zero_speed += 1
                    if zero_speed >= 2:
                        if abs(h - target) < TOLERANCE:
                            break
                        stall_retries += 1
                        if stall_retries > 3:
                            break
                        zero_speed = 0
                else:
                    zero_speed = 0
                    stall_retries = 0
        finally:
            if self._move_gen == gen:
                await self._stop_writes()

    async def hold(self, direction: str | None) -> None:
        """'up'/'down' = move while held, None = release/stop."""
        self._hold_gen += 1
        if direction is None:
            self._move_gen += 1
            await self._stop_writes()
            return
        if not self.client or not self.client.is_connected:
            raise DeskError("err_not_connected")
        self._move_gen += 1  # cancel a running target move
        gen = self._hold_gen
        cmd = CMD_UP if direction == "up" else CMD_DOWN
        while self._hold_gen == gen:
            try:
                await self._write(UUID_COMMAND, cmd, response=False)
            except Exception:
                return
            await asyncio.sleep(0.4)
        if self._hold_gen == gen:  # loop was ended from the outside
            await self._stop_writes()

    async def stop_all(self) -> None:
        self._move_gen += 1
        self._hold_gen += 1
        await self._stop_writes()

    def submit_move(self, target: float) -> None:
        self.submit(self.move_to_target(target))

    def submit_hold(self, direction: str | None) -> None:
        self.submit(self.hold(direction))

    def submit_stop(self) -> None:
        self.submit(self.stop_all())


# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------
def fmt_cm(meters: float | None) -> str:
    if meters is None:
        return "— cm"
    txt = f"{meters * 100:.1f} cm"
    return txt.replace(".", ",") if LANG == "de" else txt


def fmt_mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# Windows: is the workstation locked?
# ---------------------------------------------------------------------------
def workstation_locked() -> bool:
    """True if the Windows workstation is locked (or a secure desktop such
    as a UAC prompt is active, or screensaver is running). On other systems: False.

    Uses three signals combined:
    1. WTS session status (WTSQuerySessionInformation with WTSSessionInfoEx) -
       toggles exactly with Win+L on Windows 10/11
    2. Secure Desktop (OpenInputDesktop) - catches UAC prompts
    3. Screensaver running - pauses timer when screensaver is active
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        # WTS API constants
        WTS_CURRENT_SERVER_HANDLE = None
        WTSSessionInfoEx = 17
        WTS_CONNECTSTATE_CLASS = 1  # For compatibility check

        # Get current session ID
        kernel32 = ctypes.windll.kernel32
        session_id = kernel32.WTSGetActiveConsoleSessionId()

        # Query session information with WTSSessionInfoEx
        wtsapi32 = ctypes.windll.Wtsapi32
        buffer = ctypes.c_void_p()
        bytes_returned = wintypes.DWORD()

        result = wtsapi32.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE,
            session_id,
            WTSSessionInfoEx,
            ctypes.byref(buffer),
            ctypes.byref(bytes_returned)
        )

        if result and buffer.value and bytes_returned.value >= 20:
            # WTSSessionInfoEx returns a WTSINFOEX_LEVEL1 structure
            # Level is at offset 0 (4 bytes), SessionId at offset 4 (4 bytes)
            # SessionFlags at offset 16 (4 bytes)
            import struct
            level = struct.unpack_from('<I', ctypes.cast(buffer.value, ctypes.POINTER(ctypes.c_ubyte)), 0)[0]
            sid = struct.unpack_from('<I', ctypes.cast(buffer.value, ctypes.POINTER(ctypes.c_ubyte)), 4)[0]
            flags = struct.unpack_from('<I', ctypes.cast(buffer.value, ctypes.POINTER(ctypes.c_ubyte)), 16)[0]

            # Validate: Level must be 1, SessionId must match current
            if level == 1 and sid == session_id:
                # WTS_SESSIONSTATE_LOCK = 0x00000001 (session is locked)
                if flags & 0x00000001:
                    wtsapi32.WTSFreeMemory(buffer.value)
                    return True

            wtsapi32.WTSFreeMemory(buffer.value)

        # Fallback 1: Check input desktop name - when locked, it's "Winlogon"
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not handle:
            return True
        try:
            size = wintypes.DWORD()
            user32.GetUserObjectInformationW(handle, 2, None, 0, ctypes.byref(size))  # UOI_NAME=2
            if size.value > 0:
                buf = ctypes.create_unicode_buffer(size.value // 2 + 1)
                if user32.GetUserObjectInformationW(handle, 2, buf, size.value, ctypes.byref(size)):
                    if buf.value.lower() == "winlogon":
                        return True
        finally:
            user32.CloseDesktop(handle)

        # Fallback 2: Enumerate all windows to find lock screen (LogonUI.exe/winlogon.exe)
        # This is more reliable on Windows 10/11 as WTS may have a delay
        try:
            def enum_callback(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    h_process = kernel32.OpenProcess(0x0400 | 0x0010, False, pid.value)
                    if h_process:
                        try:
                            name_buffer = ctypes.create_unicode_buffer(1024)
                            size = wintypes.DWORD(1024)
                            if kernel32.QueryFullProcessImageNameW(h_process, 0, name_buffer, ctypes.byref(size)):
                                process_name = name_buffer.value.lower()
                                if 'logonui.exe' in process_name or 'winlogon.exe' in process_name:
                                    ctypes.cast(lparam, ctypes.POINTER(wintypes.BOOL))[0] = True
                                    return False  # Stop enumeration
                        finally:
                            kernel32.CloseHandle(h_process)
                return True

            found = wintypes.BOOL(False)
            EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.BOOL))
            user32.EnumWindows(EnumWindowsProc(enum_callback), ctypes.byref(found))
            if found.value:
                return True
        except Exception:
            pass

        # Fallback 3: Screensaver check
        try:
            system_parameters_info = user32.SystemParametersInfoW
            SPI_GETSCREENSAVERRUNNING = 0x0072
            screensaver_running = wintypes.BOOL()
            if system_parameters_info(SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(screensaver_running), 0):
                if screensaver_running.value:
                    return True
        except Exception:
            pass

        return False
    except Exception:  # noqa: BLE001
        # If WTS API fails, fall back to old OpenInputDesktop method
        try:
            import ctypes as ct
            user32 = ct.windll.user32
            handle = user32.OpenInputDesktop(0, False, 0x0001)
            if not handle:
                return True
            user32.CloseDesktop(handle)
            return False
        except Exception:
            return False


# ---------------------------------------------------------------------------
# System tray icon (optional dependency: pystray + Pillow)
# ---------------------------------------------------------------------------
class TrayIcon:
    """System tray icon with a right-click menu. Runs in its own thread;
    actions are marshalled into the tkinter main loop via the UI queue."""

    def __init__(self, app: "App") -> None:
        self.app = app
        self.icon = None
        self.running = False
        self._connected = False

    def start(self) -> None:
        import pystray  # optional dependency

        self.icon = pystray.Icon(APP_ID, self._make_image(self._connected),
                                 app_name(), self._build_menu())
        self.icon.run_detached()
        self.running = True

    def stop(self) -> None:
        self.running = False
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.stop()

    def refresh(self) -> None:
        """Rebuilds the tray menu. Necessary when states outside the tray
        change (e.g. in the main window): not every platform re-evaluates
        the callables when the menu is opened (pystray docs)."""
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.update_menu()

    def set_tooltip(self, text: str) -> None:
        """Updates the tooltip (text shown on hover)."""
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.title = text

    def set_connected(self, connected: bool) -> None:
        """Switches the icon color: blue = connected, red = disconnected."""
        if self.icon is None or connected == self._connected:
            return
        self._connected = connected
        with contextlib.suppress(Exception):
            self.icon.icon = self._make_image(connected)

    def _make_image(self, connected: bool = True):
        """Creates the tray icon (desk symbol) without an image file.
        Blue background when connected, red when not."""
        from PIL import Image, ImageDraw

        bg = (28, 100, 220, 255) if connected else (200, 30, 40, 255)
        img = Image.new("RGBA", (64, 64), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([14, 20, 50, 27], fill="white")  # desk top
        d.rectangle([18, 27, 24, 56], fill="white")  # left leg
        d.rectangle([40, 27, 46, 56], fill="white")  # right leg
        d.polygon([(32, 6), (39, 14), (25, 14)], fill="white")  # up arrow
        return img

    def _build_menu(self):
        import pystray

        app = self.app
        ui = app.submit_ui

        # All labels are callables so that a menu refresh picks up the
        # current language and current state.
        return pystray.Menu(
            pystray.MenuItem(
                lambda _t: tr("tray_open"),
                lambda _i, _t: ui(app._restore_window),
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _t: tr("tray_stop_desk"),
                lambda _i, _t: ui(app._stop_desk),
            ),
            pystray.MenuItem(
                lambda _t: tr("tray_sit_pos", name=app.sit_combo.get() or "—"),
                lambda _i, _t: ui(lambda: app._tray_move("sit")),
                enabled=lambda _t: bool(app.sit_combo.get()),
            ),
            pystray.MenuItem(
                lambda _t: tr("tray_stand_pos", name=app.stand_combo.get() or "—"),
                lambda _i, _t: ui(lambda: app._tray_move("stand")),
                enabled=lambda _t: bool(app.stand_combo.get()),
            ),
            pystray.MenuItem(
                lambda _t: app._timer_toggle_text(),
                lambda _i, _t: ui(app._timer_toggle),
            ),
            pystray.MenuItem(
                lambda _t: tr("tray_timer_stop"),
                lambda _i, _t: ui(app._timer_stop),
                enabled=lambda _t: app.timer_running,
            ),
            pystray.MenuItem(
                lambda _t: tr("tray_snooze"),
                lambda _i, _t: ui(app._timer_snooze),
                enabled=lambda _t: app.timer_running,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _t: tr("tray_quit"),
                lambda _i, _t: ui(app._quit_app),
            ),
        )


class App(tk.Tk):
    def __init__(self, config: dict) -> None:
        global LANG
        lang = config.get("language")
        LANG = lang if lang in STRINGS else "de"

        super().__init__()
        self.title(tr("app_title"))
        self.minsize(560, 620)

        self.config = config
        self.events: queue.Queue = queue.Queue()
        self.ble = BleThread()
        self.ble.start()
        self.backend = DeskBackend(self.ble, self.events)
        self.backend.auto_reconnect = bool(config.get("auto_reconnect", True))

        self.height_m: float | None = None
        self.timer_running = False
        self.timer_paused = False
        self._warned = False  # warning already shown for the current phase
        self._outside_window = False  # currently outside work hours
        self._locked = False  # Windows workstation currently locked
        self._lock_frozen = False  # timer frozen because of the lock
        self.phase: str | None = None  # "sit" | "stand" | None
        self.remaining = 0  # seconds until the next switch
        self._key_dir: str | None = None  # arrow key currently held
        self._ui_queue: queue.Queue = queue.Queue()  # calls from the tray thread
        self._i18n_widgets: list = []  # (widget, key) for live re-translation
        self._tray = TrayIcon(self)
        self._win_toaster = None  # Windows: InteractableWindowsToaster (lazy)
        self._lock_thread = None  # Background thread for lock detection
        self._lock_stop = threading.Event()  # Signal to stop lock thread

        self._build_ui()
        self._refresh_positions_ui()

        self.bind("<Unmap>", self._on_unmap)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.config.get("minimize_to_tray", True):
            self.after(300, self._start_tray)  # start once the UI is up
        if (
            self.config.get("mac", "").strip()
            and self.config.get("auto_connect", True)
        ):
            self.after(600, self._connect)  # auto-connect on app start
        self.after(100, self._poll_events)
        self.after(1000, self._tick_timer)
        if sys.platform.startswith("win"):
            self._start_lock_thread()
        else:
            self.after(500, self._poll_lock)

    # ------------------------------------------------------------- UI ---
    def _reg(self, widget, key: str):
        """Registers a static widget text for live re-translation."""
        self._i18n_widgets.append((widget, key))
        return widget

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # -- Connection --------------------------------------------------------
        conn = self._reg(
            ttk.LabelFrame(outer, text=tr("frame_connection"), padding=8),
            "frame_connection",
        )
        conn.pack(fill="x")

        self.mac_var = tk.StringVar(value=str(self.config.get("mac", "")))
        self._reg(ttk.Label(conn, text=tr("lbl_mac")), "lbl_mac").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(conn, textvariable=self.mac_var, width=19).grid(row=0, column=1, padx=6)
        self.connect_btn = self._reg(
            ttk.Button(conn, text=tr("btn_connect"), command=self._connect),
            "btn_connect",
        )
        self.connect_btn.grid(row=0, column=2, padx=4)
        self.disconnect_btn = self._reg(
            ttk.Button(
                conn, text=tr("btn_disconnect"), command=self._disconnect, state="disabled"
            ),
            "btn_disconnect",
        )
        self.disconnect_btn.grid(row=0, column=3, padx=4)
        self.pair_btn = self._reg(
            ttk.Button(conn, text=tr("btn_pair"), command=self._pair, state="disabled"),
            "btn_pair",
        )
        self.pair_btn.grid(row=0, column=4)

        self.reconnect_var = tk.BooleanVar(value=self.backend.auto_reconnect)
        self._reg(
            ttk.Checkbutton(
                conn,
                text=tr("chk_reconnect"),
                variable=self.reconnect_var,
                command=self._on_reconnect_pref,
            ),
            "chk_reconnect",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.auto_connect_var = tk.BooleanVar(
            value=bool(self.config.get("auto_connect", True))
        )
        self._reg(
            ttk.Checkbutton(
                conn,
                text=tr("chk_autoconnect"),
                variable=self.auto_connect_var,
                command=self._on_auto_connect_pref,
            ),
            "chk_autoconnect",
        ).grid(row=1, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=(4, 0))
        self.status_var = tk.StringVar(value=tr("status_not_connected"))
        self.status_lbl = ttk.Label(conn, textvariable=self.status_var)
        self.status_lbl.grid(row=2, column=0, columnspan=5, sticky="e", pady=(2, 0))

        # -- Height & manual control -------------------------------------------
        height = self._reg(
            ttk.LabelFrame(outer, text=tr("frame_desk"), padding=8), "frame_desk"
        )
        height.pack(fill="x", pady=(8, 0))

        self.height_var = tk.StringVar(value=fmt_cm(None))
        ttk.Label(
            height, textvariable=self.height_var, font=("TkDefaultFont", 26, "bold")
        ).grid(row=0, column=0, rowspan=2, padx=(0, 24), sticky="w")

        self.up_btn = self._hold_button(height, tr("btn_up"), "up")
        self.down_btn = self._hold_button(height, tr("btn_down"), "down")
        self._reg(self.up_btn, "btn_up").grid(row=0, column=1, sticky="ew", padx=2)
        self._reg(self.down_btn, "btn_down").grid(row=1, column=1, sticky="ew", padx=2)
        height.columnconfigure(1, weight=1)

        self.stop_btn = self._reg(
            tk.Button(height, text=tr("btn_stop"), takefocus=0, command=self._stop_desk),
            "btn_stop",
        )
        self.stop_btn.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(8, 0))

        # -- Saved positions ----------------------------------------------------
        pf = self._reg(
            ttk.LabelFrame(outer, text=tr("frame_positions"), padding=8),
            "frame_positions",
        )
        pf.pack(fill="both", expand=True, pady=(8, 0))
        pf.columnconfigure(0, weight=1)
        pf.rowconfigure(3, weight=1)

        self.pos_list = tk.Listbox(pf, height=4)
        self.pos_list.bind("<Double-Button-1>", lambda _e: self._goto_selected())
        self.pos_list.grid(row=0, column=0, rowspan=4, sticky="nsew")

        self.goto_btn = self._reg(
            ttk.Button(pf, text=tr("btn_goto"), command=self._goto_selected), "btn_goto"
        )
        self.goto_btn.grid(row=0, column=1, sticky="ew", padx=4, pady=1)
        self.save_pos_btn = self._reg(
            ttk.Button(pf, text=tr("btn_save_pos"), command=self._save_position),
            "btn_save_pos",
        )
        self.save_pos_btn.grid(row=1, column=1, sticky="ew", padx=4, pady=1)
        self.del_pos_btn = self._reg(
            ttk.Button(pf, text=tr("btn_delete_pos"), command=self._delete_position),
            "btn_delete_pos",
        )
        self.del_pos_btn.grid(row=2, column=1, sticky="ew", padx=4, pady=1)
        self._reg(ttk.Label(pf, text=tr("lbl_doubleclick")), "lbl_doubleclick").grid(
            row=3, column=1, sticky="ew", padx=4
        )

        # -- Timer ----------------------------------------------------------------
        tf = self._reg(
            ttk.LabelFrame(outer, text=tr("frame_timer"), padding=8), "frame_timer"
        )
        tf.pack(fill="x", pady=(8, 0))

        timer_cfg = self.config["timer"]
        self.sit_combo = ttk.Combobox(tf, state="readonly", width=16)
        self.stand_combo = ttk.Combobox(tf, state="readonly", width=16)
        self.sit_min_var = tk.StringVar(value=self._fmt_num(timer_cfg["sit_min"]))
        self.stand_min_var = tk.StringVar(value=self._fmt_num(timer_cfg["stand_min"]))
        self.auto_move_var = tk.BooleanVar(value=bool(timer_cfg["auto_move"]))
        self.warn_var = tk.BooleanVar(value=bool(timer_cfg.get("warn", True)))
        self.warn_min_var = tk.StringVar(
            value=self._fmt_num(timer_cfg.get("warn_min", 1.0))
        )
        self.vary_var = tk.BooleanVar(value=bool(timer_cfg.get("vary", False)))
        self.work_var = tk.BooleanVar(value=bool(timer_cfg.get("work_enabled", False)))
        self.weekday_var = tk.BooleanVar(value=bool(timer_cfg.get("work_weekdays", True)))
        self.work_start_var = tk.StringVar(value=str(timer_cfg.get("work_start", "09:00")))
        self.work_end_var = tk.StringVar(value=str(timer_cfg.get("work_end", "17:00")))
        self.lock_pause_var = tk.BooleanVar(value=bool(timer_cfg.get("pause_on_lock", True)))

        self._reg(ttk.Label(tf, text=tr("lbl_sit_pos")), "lbl_sit_pos").grid(
            row=0, column=0, sticky="w"
        )
        self.sit_combo.grid(row=0, column=1, padx=4)
        self._reg(ttk.Label(tf, text=tr("lbl_duration")), "lbl_duration").grid(
            row=0, column=2, padx=(12, 0)
        )
        ttk.Entry(tf, textvariable=self.sit_min_var, width=6).grid(row=0, column=3)

        self._reg(ttk.Label(tf, text=tr("lbl_stand_pos")), "lbl_stand_pos").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        self.stand_combo.grid(row=1, column=1, padx=4, pady=(4, 0))
        self._reg(ttk.Label(tf, text=tr("lbl_duration")), "lbl_duration").grid(
            row=1, column=2, padx=(12, 0), pady=(4, 0)
        )
        ttk.Entry(tf, textvariable=self.stand_min_var, width=6).grid(
            row=1, column=3, pady=(4, 0)
        )

        self._reg(
            ttk.Checkbutton(
                tf,
                text=tr("chk_auto_move"),
                variable=self.auto_move_var,
            ),
            "chk_auto_move",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self._reg(
            ttk.Checkbutton(tf, text=tr("chk_warn"), variable=self.warn_var), "chk_warn"
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Entry(tf, textvariable=self.warn_min_var, width=5).grid(row=3, column=2)
        self._reg(ttk.Label(tf, text=tr("lbl_min_before")), "lbl_min_before").grid(
            row=3, column=3, sticky="w", padx=4
        )

        self._reg(
            ttk.Checkbutton(tf, text=tr("chk_vary"), variable=self.vary_var), "chk_vary"
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._reg(
            ttk.Checkbutton(
                tf,
                text=tr("chk_lock_pause"),
                variable=self.lock_pause_var,
                command=self._on_lock_pref,
            ),
            "chk_lock_pause",
        ).grid(row=4, column=2, columnspan=3, sticky="w", pady=(4, 0), padx=(12, 0))
        self._reg(
            ttk.Checkbutton(tf, text=tr("chk_work_hours"), variable=self.work_var),
            "chk_work_hours",
        ).grid(row=5, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(tf, textvariable=self.work_start_var, width=7).grid(row=5, column=1)
        ttk.Label(tf, text="–").grid(row=5, column=2)
        ttk.Entry(tf, textvariable=self.work_end_var, width=7).grid(row=5, column=3)
        self._reg(
            ttk.Checkbutton(tf, text=tr("chk_weekdays"), variable=self.weekday_var),
            "chk_weekdays",
        ).grid(row=5, column=4, sticky="w", padx=8)

        self.countdown_var = tk.StringVar(value=tr("msg_timer_stopped"))
        ttk.Label(tf, textvariable=self.countdown_var, font=("TkDefaultFont", 11, "bold")).grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        self.timer_start_btn = self._reg(
            ttk.Button(tf, text=tr("btn_start_timer"), command=self._timer_start),
            "btn_start_timer",
        )
        self.timer_start_btn.grid(row=7, column=0, sticky="w", pady=(4, 0))
        self.timer_pause_btn = ttk.Button(
            tf, text=tr("btn_pause"), command=self._timer_pause_toggle, state="disabled"
        )
        self.timer_pause_btn.grid(row=7, column=1, sticky="w", pady=(4, 0))
        self.timer_stop_btn = self._reg(
            ttk.Button(
                tf, text=tr("btn_stop_timer"), command=self._timer_stop, state="disabled"
            ),
            "btn_stop_timer",
        )
        self.timer_stop_btn.grid(row=7, column=2, sticky="w", pady=(4, 0))
        self.snooze_btn = self._reg(
            ttk.Button(tf, text=tr("btn_snooze5"), command=self._timer_snooze, state="disabled"),
            "btn_snooze5",
        )
        self.snooze_btn.grid(row=7, column=3, sticky="w", pady=(4, 0))
        self.switch_now_btn = self._reg(
            ttk.Button(tf, text=tr("btn_switch_now"), command=self._switch_now),
            "btn_switch_now",
        )
        self.switch_now_btn.grid(row=7, column=4, sticky="e", pady=(4, 0))

        # -- Settings ------------------------------------------------------------
        settings = self._reg(
            ttk.LabelFrame(outer, text=tr("frame_settings"), padding=8), "frame_settings"
        )
        settings.pack(fill="x", pady=(8, 0))
        self.tray_var = tk.BooleanVar(
            value=bool(self.config.get("minimize_to_tray", True))
        )
        self._reg(
            ttk.Checkbutton(
                settings,
                text=tr("chk_tray"),
                variable=self.tray_var,
                command=self._on_tray_pref,
            ),
            "chk_tray",
        ).grid(row=0, column=0, sticky="w")
        self.autostart_var = tk.BooleanVar(value=autostart_is_enabled())
        self._reg(
            ttk.Checkbutton(
                settings,
                text=tr("chk_autostart"),
                variable=self.autostart_var,
                command=self._on_autostart_pref,
            ),
            "chk_autostart",
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self._reg(ttk.Label(settings, text=tr("lbl_language")), "lbl_language").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        self.lang_var = tk.StringVar(value=LANGUAGES.get(LANG, "Deutsch"))
        self.lang_combo = ttk.Combobox(
            settings,
            state="readonly",
            width=12,
            textvariable=self.lang_var,
            values=list(LANGUAGES.values()),
        )
        self.lang_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_language_pref())
        self.lang_combo.grid(row=1, column=1, sticky="w", pady=(6, 0))

        # -- Keyboard: hold arrow keys -------------------------------------------
        self.bind_all("<KeyPress-Up>", lambda e: self._key_hold(e, "up"))
        self.bind_all("<KeyRelease-Up>", lambda e: self._key_hold(e, None))
        self.bind_all("<KeyPress-Down>", lambda e: self._key_hold(e, "down"))
        self.bind_all("<KeyRelease-Down>", lambda e: self._key_hold(e, None))

        # Widgets that need a connection
        self._needs_conn = [
            self.up_btn,
            self.down_btn,
            self.stop_btn,
            self.goto_btn,
            self.switch_now_btn,
            self.pair_btn,
        ]
        self._set_connected_widgets(False)

    def _hold_button(self, parent, text: str, direction: str) -> tk.Button:
        btn = tk.Button(parent, text=text, takefocus=0)
        btn.bind("<ButtonPress-1>", lambda _e: self._hold(direction))
        btn.bind("<ButtonRelease-1>", lambda _e: self._hold(None))
        btn.bind("<Leave>", lambda _e: self._hold(None))
        return btn

    # -------------------------------------------------- event processing ---
    def _poll_events(self) -> None:
        try:
            while True:
                kind, args = self.events.get_nowait()
                if kind == "height":
                    self.height_m = args[0]
                    self.height_var.set(fmt_cm(self.height_m))
                elif kind == "status":
                    self._set_status(tr(args[0], **args[1]), args[2])
                elif kind == "error":
                    self._show_error(tr(args[0], **args[1]))
        except queue.Empty:
            pass
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _set_status(self, text: str, connected: bool) -> None:
        self.status_var.set(text)
        self.status_lbl.configure(foreground="black")
        self.connect_btn.configure(state="disabled" if connected else "normal")
        self._tray.set_connected(connected)
        self._update_tray_tooltip()
        self.disconnect_btn.configure(state="normal" if connected else "disabled")
        self._set_connected_widgets(connected)

    def _show_error(self, msg: str) -> None:
        self.status_var.set(tr("err_status", msg=msg.splitlines()[0]))
        self.status_lbl.configure(foreground="#b00020")
        if not self.backend.connected:
            self.connect_btn.configure(state="normal")
            self.disconnect_btn.configure(state="disabled")
        else:
            self.pair_btn.configure(state="normal")

    def _set_connected_widgets(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in self._needs_conn:
            try:
                w.configure(state=state)
            except tk.TclError:
                pass

    # --------------------------------------------------------- connection ---
    def _connect(self) -> None:
        mac = self.mac_var.get().strip()
        self.config["mac"] = mac
        save_config(self.config)
        self.connect_btn.configure(state="disabled")
        self.status_var.set(tr("status_searching"))
        self.backend.submit(self.backend.connect(mac or None))

    def _disconnect(self) -> None:
        self.reconnect_var.set(False)
        self._on_reconnect_pref()
        self.backend.submit(self.backend.disconnect())

    def _pair(self) -> None:
        self.pair_btn.configure(state="disabled")
        self.status_var.set(tr("status_pairing"))
        self.backend.submit(self.backend.pair())

    def _on_reconnect_pref(self) -> None:
        self.backend.auto_reconnect = bool(self.reconnect_var.get())
        self.config["auto_reconnect"] = self.backend.auto_reconnect
        save_config(self.config)

    def _on_auto_connect_pref(self) -> None:
        self.config["auto_connect"] = bool(self.auto_connect_var.get())
        save_config(self.config)

    # ------------------------------------------------------------ movement ---
    def _hold(self, direction: str | None) -> None:
        if direction and not self.backend.connected:
            return
        self.backend.submit_hold(direction)

    def _key_hold(self, event, direction: str | None) -> None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Listbox, tk.Text)):
            return
        if direction is not None:
            if self._key_dir == direction:  # ignore auto-repeat
                return
            self._key_dir = direction
        else:
            self._key_dir = None
        self._hold(direction)

    def _stop_desk(self) -> None:
        self.backend.submit_stop()

    def _move_to(self, height_m: float) -> None:
        if not self.backend.connected:
            self._show_error(tr("err_not_connected"))
            return
        self.backend.submit_move(height_m)

    # --------------------------------------------------------- positions ---
    def _selected_position(self):
        sel = self.pos_list.curselection()
        if not sel:
            return None
        idx = sel[0]
        positions = self.config["positions"]
        if 0 <= idx < len(positions):
            return idx
        return None

    def _goto_selected(self) -> None:
        idx = self._selected_position()
        if idx is None:
            messagebox.showinfo("IDÅSEN", tr("dlg_select_position"))
            return
        _name, height = self.config["positions"][idx]
        self._move_to(height)

    def _save_position(self) -> None:
        name = simpledialog.askstring(
            tr("dlg_save_position_title"), tr("dlg_pos_name"), parent=self
        )
        if not name:
            return
        if self.height_m is not None:
            height = self.height_m
        else:
            raw = simpledialog.askfloat(
                tr("dlg_save_position_title"),
                tr("dlg_height_cm"),
                parent=self,
                minvalue=50.0,
                maxvalue=140.0,
            )
            if raw is None:
                return
            height = raw / 100.0
        if not (MIN_HEIGHT <= height <= MAX_HEIGHT):
            self._show_error(
                tr("err_height_range", min=fmt_cm(MIN_HEIGHT), max=fmt_cm(MAX_HEIGHT))
            )
            return
        positions = self.config["positions"]
        positions[:] = [[n, h] for n, h in positions if n != name]
        positions.append([name, round(height, 3)])
        self.config["positions"] = positions
        save_config(self.config)
        self._refresh_positions_ui(select_name=name)

    def _delete_position(self) -> None:
        idx = self._selected_position()
        if idx is None:
            messagebox.showinfo("IDÅSEN", tr("dlg_select_position"))
            return
        name = self.config["positions"][idx][0]
        if not messagebox.askyesno("IDÅSEN", tr("dlg_confirm_delete", name=name)):
            return
        del self.config["positions"][idx]
        save_config(self.config)
        self._refresh_positions_ui()

    def _refresh_positions_ui(self, select_name: str | None = None) -> None:
        positions = self.config["positions"]
        self.pos_list.delete(0, tk.END)
        for name, height in positions:
            self.pos_list.insert(tk.END, f"{name} – {fmt_cm(height)}")

        names = [n for n, _h in positions]
        for combo, key in ((self.sit_combo, "sit"), (self.stand_combo, "stand")):
            current = combo.get()
            combo["values"] = names
            if current in names:
                combo.set(current)
            elif self.config["timer"].get(key) in names:
                combo.set(self.config["timer"][key])
            elif names:
                combo.set(names[0])
            else:
                combo.set("")

        if select_name:
            for i, (name, _h) in enumerate(positions):
                if name == select_name:
                    self.pos_list.selection_clear(0, tk.END)
                    self.pos_list.selection_set(i)
                    self.pos_list.see(i)
                    break
        self._tray.refresh()  # update position names in the tray menu

    # -------------------------------------------------------------- timer ---
    def _timer_names(self) -> tuple[str, str]:
        return self.sit_combo.get(), self.stand_combo.get()

    def _position_height(self, name: str) -> float | None:
        for n, h in self.config["positions"]:
            if n == name:
                return h
        return None

    def _duration(self, var: tk.StringVar) -> float | None:
        try:
            minutes = float(var.get().replace(",", "."))
        except ValueError:
            return None
        if not (0 < minutes <= 24 * 60):
            return None
        return minutes

    def _validate_timer(self) -> tuple[str, str, float, float] | None:
        sit_name, stand_name = self._timer_names()
        sit_min = self._duration(self.sit_min_var)
        stand_min = self._duration(self.stand_min_var)
        if not sit_name or not stand_name:
            messagebox.showwarning("Timer", tr("dlg_choose_positions"))
            return None
        if sit_min is None or stand_min is None:
            messagebox.showwarning("Timer", tr("dlg_invalid_durations"))
            return None
        self.config["timer"].update(
            sit=sit_name,
            stand=stand_name,
            sit_min=sit_min,
            stand_min=stand_min,
            auto_move=bool(self.auto_move_var.get()),
            warn=bool(self.warn_var.get()),
            warn_min=self._duration(self.warn_min_var) or 1.0,
            vary=bool(self.vary_var.get()),
            work_enabled=bool(self.work_var.get()),
            work_start=self.work_start_var.get().strip(),
            work_end=self.work_end_var.get().strip(),
            work_weekdays=bool(self.weekday_var.get()),
        )
        save_config(self.config)
        return sit_name, stand_name, sit_min, stand_min

    def _timer_start(self) -> None:
        data = self._validate_timer()
        if data is None:
            return
        sit_name, _stand_name, sit_min, _stand_min = data
        # Start phase: the position closer to the current height
        sit_h = self._position_height(sit_name)
        if self.height_m is not None and sit_h is not None:
            self.phase = "sit" if self.height_m <= sit_h + 0.05 else "stand"
        else:
            self.phase = "sit"
        self.remaining = self._phase_seconds(sit_min if self.phase == "sit" else data[3])
        self.timer_running = True
        self.timer_paused = False
        self._warned = False
        self._outside_window = False
        self.timer_start_btn.configure(state="disabled")
        self.timer_pause_btn.configure(text=tr("btn_pause"), state="normal")
        self.timer_stop_btn.configure(state="normal")
        self.snooze_btn.configure(state="normal")
        if self.auto_move_var.get():
            self._move_to_phase(self.phase)
        self._update_countdown_label()
        self._tray.refresh()

    def _timer_stop(self) -> None:
        self.timer_running = False
        self.timer_paused = False
        self.countdown_var.set(tr("msg_timer_stopped"))
        self.timer_start_btn.configure(state="normal")
        self.timer_pause_btn.configure(text=tr("btn_pause"), state="disabled")
        self.timer_stop_btn.configure(state="disabled")
        self.snooze_btn.configure(state="disabled")
        self._tray.refresh()

    def _timer_pause(self) -> None:
        if self.timer_running and not self.timer_paused:
            self.timer_paused = True
            self.timer_pause_btn.configure(text=tr("btn_resume"))
            self._update_countdown_label()
            self._tray.refresh()

    def _timer_resume(self) -> None:
        if self.timer_running and self.timer_paused:
            self.timer_paused = False
            self.timer_pause_btn.configure(text=tr("btn_pause"))
            self._update_countdown_label()
            self._tray.refresh()

    def _timer_pause_toggle(self) -> None:
        if not self.timer_running:
            return
        self._timer_resume() if self.timer_paused else self._timer_pause()

    def _timer_toggle(self) -> None:
        """From the tray: start / pause / resume."""
        if not self.timer_running:
            self._timer_start()
        else:
            self._timer_pause_toggle()

    def _timer_toggle_text(self) -> str:
        if not self.timer_running:
            return tr("tray_timer_start")
        return tr("tray_timer_resume") if self.timer_paused else tr("tray_timer_pause")

    def _phase_seconds(self, minutes: float) -> int:
        """Duration in seconds; optionally varied slightly (±20 %)."""
        seconds = minutes * 60
        if self.vary_var.get():
            seconds *= random.uniform(0.8, 1.2)
        return max(1, int(seconds))

    @staticmethod
    def _parse_hhmm(text: str, default: int) -> int:
        """'HH:MM' -> minutes since midnight; `default` on errors."""
        try:
            h_str, m_str = text.strip().split(":")
            h, m = int(h_str), int(m_str)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except ValueError:
            pass
        return default

    def _in_work_window(self) -> bool:
        if not self.work_var.get():
            return True
        now = datetime.now()
        if self.weekday_var.get() and now.weekday() >= 5:
            return False
        current = now.hour * 60 + now.minute
        start = self._parse_hhmm(self.work_start_var.get(), 9 * 60)
        end = self._parse_hhmm(self.work_end_var.get(), 17 * 60)
        return start <= current <= end

    def _notify(
        self, message: str, title: str | None = None, snooze: bool = False
    ) -> None:
        """System notification.
        Windows with 'windows-toasts' (optional): a real toast with buttons
        ("+5 min" / "Switch now"). Fallback: pystray (text only) or the
        status bar."""
        if title is None:
            title = app_name()
        hint = tr("notify_snooze_hint") if snooze else ""
        if sys.platform.startswith("win"):
            with contextlib.suppress(Exception):
                self._show_windows_toast(message, title, snooze)
                return
        if self._tray.running and self._tray.icon is not None:
            with contextlib.suppress(Exception):
                self._tray.icon.notify(message + hint, title)
                return
        self.status_var.set(message + hint)

    def _show_windows_toast(self, message: str, title: str, snooze: bool) -> None:
        """Windows toast with buttons (requires 'pip install windows-toasts')."""
        from windows_toasts import InteractableWindowsToaster, Toast, ToastButton

        if self._win_toaster is None:
            self._win_toaster = InteractableWindowsToaster(app_name())
        toast = Toast()
        toast.text_fields = [title, message]
        if snooze:
            toast.AddAction(ToastButton(tr("btn_snooze5"), "snooze"))
            toast.AddAction(ToastButton(tr("btn_switch_now_toast"), "switch"))
        toast.on_activated = self._on_toast_activated
        self._win_toaster.show_toast(toast)

    def _on_toast_activated(self, event_args) -> None:
        """Button click in the Windows notification (runs in the WinRT
        thread); marshalled into the tkinter main loop."""
        arguments = getattr(event_args, "arguments", "") or ""
        if "snooze" in arguments:
            self.submit_ui(self._timer_snooze)
        elif "switch" in arguments:
            self.submit_ui(self._switch_now)
        else:
            self.submit_ui(self._restore_window)  # click on the toast text

    def _maybe_warn(self) -> None:
        if self._warned or not self.warn_var.get():
            return
        warn_min = self._duration(self.warn_min_var) or 1.0
        if self.remaining <= warn_min * 60:
            self._warned = True
            pos = self.stand_combo.get() if self.phase == "sit" else self.sit_combo.get()
            self._notify(
                tr("notify_switch_msg", pos=pos, time=fmt_mmss(self.remaining)),
                tr("notify_switch_title"),
                snooze=True,
            )

    def _timer_snooze(self) -> None:
        """Postpones the upcoming switch by 5 minutes."""
        if not self.timer_running:
            return
        self.remaining += 5 * 60
        self._warned = False
        self._update_countdown_label()
        self._tray.refresh()

    def _tick_timer(self) -> None:
        if self.timer_running and not self.timer_paused:
            outside = self.work_var.get() and not self._in_work_window()
            locked = self.lock_pause_var.get() and self._locked
            if outside or locked:
                # Frozen (work hours / screen lock)
                if (outside, locked) != (self._outside_window, self._lock_frozen):
                    self._outside_window, self._lock_frozen = outside, locked
                    self._update_countdown_label()
            else:
                self._outside_window = False
                self._lock_frozen = False
                self.remaining -= 1
                if self.remaining <= 0:
                    self._switch_phase()
                else:
                    self._maybe_warn()
                    self._update_countdown_label()
        self._update_tray_tooltip()
        self.after(1000, self._tick_timer)

    def _poll_lock(self) -> None:
        locked = workstation_locked()
        if locked != self._locked:
            self._locked = locked
            self._update_countdown_label()
            self._update_tray_tooltip()
        self.after(500, self._poll_lock)  # Poll every 500ms for faster lock detection

    def _start_lock_thread(self) -> None:
        """Start a background thread that uses WTS notifications for instant lock detection."""
        if self._lock_thread is not None:
            return
        self._lock_stop.clear()
        self._lock_thread = threading.Thread(target=self._lock_loop, daemon=True)
        self._lock_thread.start()

    def _stop_lock_thread(self) -> None:
        """Stop the lock detection thread."""
        if self._lock_thread is not None:
            self._lock_stop.set()
            self._lock_thread.join(timeout=2.0)
            self._lock_thread = None

    def _lock_loop(self) -> None:
        """Background thread using WTSRegisterSessionNotification for instant lock/unlock events.
        
        Creates a message-only window, registers it for WTS session notifications,
        and runs a message loop. When WM_WTSSESSION_CHANGE is received with
        WTS_SESSION_LOCK (7) or WTS_SESSION_UNLOCK (8), updates the lock state.
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        wtsapi32 = ctypes.windll.Wtsapi32
        kernel32 = ctypes.windll.kernel32

        WM_WTSSESSION_CHANGE = 0x02B1
        WTS_SESSION_LOCK = 7
        WTS_SESSION_UNLOCK = 8
        WTS_CONSOLE_CONNECT = 1
        WTS_CONSOLE_DISCONNECT = 2
        WTS_REMOTE_CONNECT = 3
        WTS_REMOTE_DISCONNECT = 4

        # Window procedure that handles WM_WTSSESSION_CHANGE
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_WTSSESSION_CHANGE:
                if wparam == WTS_SESSION_LOCK:
                    self.after(0, lambda: self._set_locked(True))
                elif wparam == WTS_SESSION_UNLOCK:
                    self.after(0, lambda: self._set_locked(False))
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        wnd_proc_cb = WNDPROC(wnd_proc)

        # Register window class (define WNDCLASSEXW manually - not in Python 3.10 wintypes)
        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = wnd_proc_cb
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "IdasenLockMonitor"
        class_atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not class_atom:
            # Fallback to polling if window creation fails
            self._lock_poll_fallback()
            return

        # Create message-only window (HWND_MESSAGE = -3)
        HWND_MESSAGE = wintypes.HWND(-3)
        hwnd = user32.CreateWindowExW(
            0, "IdasenLockMonitor", "IdasenLockMonitor",
            0, 0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None
        )
        if not hwnd:
            self._lock_poll_fallback()
            return

        # Register for WTS session notifications
        wts_registered = bool(wtsapi32.WTSRegisterSessionNotification(hwnd, 0))  # 0 = all sessions

        # Initial state check
        self.after(0, lambda: self._set_locked(workstation_locked()))

        # Hybrid loop: process messages AND poll every 500ms as backup
        msg = wintypes.MSG()
        last_poll = 0
        import time as time_mod
        while not self._lock_stop.is_set():
            # Process all pending messages
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE=1
                if msg.message == 0x0012:  # WM_QUIT
                    self._lock_stop.set()
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            # Poll as backup every 500ms (in case WTS notifications don't work)
            now = time_mod.monotonic()
            if now - last_poll >= 0.5:
                last_poll = now
                try:
                    locked = workstation_locked()
                    if locked != self._locked:
                        self.after(0, lambda l=locked: self._set_locked(l))
                except Exception:
                    pass

            # Sleep briefly to avoid busy loop
            self._lock_stop.wait(0.05)

        # Cleanup
        if wts_registered:
            wtsapi32.WTSUnRegisterSessionNotification(hwnd)
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW("IdasenLockMonitor", wc.hInstance)

    def _lock_poll_fallback(self) -> None:
        """Fallback to polling if WTS notification setup fails."""
        while not self._lock_stop.is_set():
            try:
                locked = workstation_locked()
                if locked != self._locked:
                    self.after(0, lambda l=locked: self._set_locked(l))
            except Exception:
                pass
            self._lock_stop.wait(0.5)

    def _set_locked(self, locked: bool) -> None:
        """Update locked state from the main thread."""
        if locked != self._locked:
            self._locked = locked
            # Debug log
            try:
                with open(os.path.expanduser("~/.idasen_lock_debug.log"), "a") as f:
                    f.write(f"{datetime.now().isoformat()} locked={locked} remaining={self.remaining}\n")
            except Exception:
                pass
            self._update_countdown_label()
            self._update_tray_tooltip()

    def _on_lock_pref(self) -> None:
        self.config["timer"]["pause_on_lock"] = bool(self.lock_pause_var.get())
        save_config(self.config)

    def _tray_tooltip_text(self) -> str:
        """Text for the tray tooltip (mouse over the icon).
        The connection state is encoded in the icon color (blue/red)."""
        if not self.timer_running or self.phase is None:
            timer = tr("msg_timer_stopped")
        else:
            phase_txt = tr("sit_phase") if self.phase == "sit" else tr("stand_phase")
            frozen = self.timer_paused or self._outside_window or self._lock_frozen
            state = tr("paused_suffix") if frozen else ""
            timer = f"{phase_txt}, {tr('switch_in', time=fmt_mmss(self.remaining))}{state}"
        return f"IDÅSEN – {timer} | {fmt_cm(self.height_m)}"

    def _update_tray_tooltip(self) -> None:
        self._tray.set_tooltip(self._tray_tooltip_text())

    def _switch_phase(self) -> None:
        other = "stand" if self.phase == "sit" else "sit"
        self.phase = other
        dur = self._duration(self.stand_min_var if other == "stand" else self.sit_min_var)
        self.remaining = self._phase_seconds(dur or 15.0)
        self._warned = False
        if self.auto_move_var.get():
            self._move_to_phase(other)
        self._update_countdown_label()

    def _move_to_phase(self, phase: str) -> None:
        name = self.sit_combo.get() if phase == "sit" else self.stand_combo.get()
        height = self._position_height(name)
        if height is None:
            self._show_error(tr("err_pos_not_found", name=name))
            return
        if self.backend.connected:
            self.backend.submit_move(height)
        else:
            self._show_error(tr("err_switch_skipped"))

    def _switch_now(self) -> None:
        data = self._validate_timer()
        if data is None:
            return
        sit_name, stand_name, _smin, _stmin = data
        if self.timer_running:
            self._switch_phase()
        else:
            # Not running: simply move to the farther position
            sit_h = self._position_height(sit_name)
            stand_h = self._position_height(stand_name)
            if sit_h is None or stand_h is None:
                return
            if self.height_m is None:
                self._show_error(tr("err_no_height"))
                return
            self._move_to(sit_h if abs(self.height_m - sit_h) > abs(self.height_m - stand_h) else stand_h)

    def _update_countdown_label(self) -> None:
        if not self.timer_running or self.phase is None:
            self.countdown_var.set(tr("msg_timer_stopped"))
            return
        phase_txt = tr("sit_phase") if self.phase == "sit" else tr("stand_phase")
        target = self.sit_combo.get() if self.phase == "sit" else self.stand_combo.get()
        note = "" if self.auto_move_var.get() else tr("note_reminder_only")
        if self._lock_frozen:
            self.countdown_var.set(
                tr("countdown_locked", phase=phase_txt, target=target, note=note)
            )
        elif self._outside_window:
            self.countdown_var.set(
                tr("countdown_outside", phase=phase_txt, target=target, note=note)
            )
        elif self.timer_paused:
            self.countdown_var.set(
                tr("countdown_paused", phase=phase_txt, target=target,
                   time=fmt_mmss(self.remaining), note=note)
            )
        else:
            self.countdown_var.set(
                tr("countdown_switch", phase=phase_txt, target=target,
                   time=fmt_mmss(self.remaining), note=note)
            )

    @staticmethod
    def _fmt_num(value: float) -> str:
        txt = f"{float(value):g}"
        return txt.replace(".", ",") if LANG == "de" else txt

    # --------------------------------------------- language & re-translation ---
    def _on_language_pref(self) -> None:
        """Language combo: switches the UI language live."""
        global LANG
        name = self.lang_var.get()
        code = next((c for c, n in LANGUAGES.items() if n == name), "de")
        if code == LANG:
            return
        LANG = code
        self.config["language"] = code
        save_config(self.config)
        self._retranslate_ui()
        self.status_var.set(tr("status_lang_changed", lang=name))
        self.status_lbl.configure(foreground="black")

    def _retranslate_ui(self) -> None:
        """Re-translates all registered static texts into the current
        language and refreshes the dynamic parts (countdown, tooltip,
        tray menu)."""
        self.title(tr("app_title"))
        for widget, key in self._i18n_widgets:
            with contextlib.suppress(Exception):
                widget.configure(text=tr(key))
        if self.timer_running:
            self.timer_pause_btn.configure(
                text=tr("btn_resume") if self.timer_paused else tr("btn_pause")
            )
        else:
            self.timer_pause_btn.configure(text=tr("btn_pause"))
        self._update_countdown_label()
        self._update_tray_tooltip()
        self._tray.refresh()

    # ------------------------------------------- system tray & autostart ---
    def submit_ui(self, fn) -> None:
        """Runs fn in the tkinter thread (e.g. from the tray thread)."""
        self._ui_queue.put(fn)

    def _start_tray(self) -> None:
        if self._tray.running or not self.tray_var.get():
            return
        try:
            self._tray.start()
        except Exception as exc:  # noqa: BLE001
            self.tray_var.set(False)
            self.status_var.set(tr("err_systray", exc=exc))
            self.status_lbl.configure(foreground="#b00020")

    def _tray_move(self, phase: str) -> None:
        name = self.sit_combo.get() if phase == "sit" else self.stand_combo.get()
        height = self._position_height(name)
        if height is not None:
            self._move_to(height)

    def _on_unmap(self, event) -> None:
        # Minimize -> hide the window in the tray (remove from taskbar)
        if event.widget is not self:
            return
        if self.tray_var.get() and self._tray.running and self.state() == "iconic":
            self.after(150, self.withdraw)

    def _hide_to_tray(self) -> None:
        self.withdraw()

    def _restore_window(self) -> None:
        self.deiconify()
        self.lift()

    def _on_tray_pref(self) -> None:
        self.config["minimize_to_tray"] = bool(self.tray_var.get())
        save_config(self.config)
        if self.tray_var.get():
            self._start_tray()
        else:
            self._tray.stop()
            if not self.winfo_viewable():
                self._restore_window()  # otherwise the app would be invisible

    def _on_autostart_pref(self) -> None:
        try:
            if self.autostart_var.get():
                autostart_enable()
            else:
                autostart_disable()
            self.status_var.set(
                tr("status_autostart_on") if self.autostart_var.get()
                else tr("status_autostart_off")
            )
            self.status_lbl.configure(foreground="black")
        except OSError as exc:
            self.autostart_var.set(autostart_is_enabled())
            self._show_error(tr("err_autostart", exc=exc))

    def _quit_app(self) -> None:
        self._on_close(force=True)

    # ------------------------------------------------------------- quit ---
    def _sync_config_from_ui(self) -> None:
        self.config["mac"] = self.mac_var.get().strip()
        self.config["language"] = LANG
        self.config["auto_reconnect"] = bool(self.reconnect_var.get())
        self.config["auto_connect"] = bool(self.auto_connect_var.get())
        self.config["minimize_to_tray"] = bool(self.tray_var.get())
        self.config["timer"].update(
            sit=self.sit_combo.get(),
            stand=self.stand_combo.get(),
            sit_min=self._duration(self.sit_min_var) or self.config["timer"]["sit_min"],
            stand_min=self._duration(self.stand_min_var) or self.config["timer"]["stand_min"],
            auto_move=bool(self.auto_move_var.get()),
            pause_on_lock=bool(self.lock_pause_var.get()),
        )

    def _on_close(self, force: bool = False) -> None:
        # On ×: minimize to the tray instead of quitting (if the tray is active)
        if not force and self.tray_var.get() and self._tray.running:
            self._hide_to_tray()
            return
        self._sync_config_from_ui()
        save_config(self.config)
        self.backend.auto_reconnect = False
        self.backend.submit(self.backend.disconnect())
        self._tray.stop()
        self.ble.stop()
        self._stop_lock_thread()
        self.destroy()


def main() -> None:
    app = App(load_config())
    app.mainloop()


if __name__ == "__main__":
    main()
