#!/usr/bin/env python3
"""Simple PoE2 macro overlay.

While the macro is ON:
  * Key '1' is spammed at a tunable interval (gauss(mean, 10% of mean)),
    with each key running in its own thread.
  * Pressing 'E' triggers 'F' to be sent ~0.49 s later, clamped to 0.47-0.51 s.

A tiny always-on-top overlay shows ON/OFF state and saves its position +
per-key intervals to ~/.poe2_macro_state.json between runs.

Toggle uses a Windows low-level keyboard hook that filters injected events,
so the macro's own keypresses do NOT trigger themselves.

Usage:
    python poe2_macro.py

Global hotkeys (work from any window):
    Shift+4 (real press)   toggle ON / OFF
    Shift+5 (real press)   quit
    Ctrl+C                 quit (from the launching terminal)

Click the overlay (no drag) to open per-key sliders; click again to collapse.
Drag the overlay header to move it.
"""
import ctypes
import ctypes.wintypes as wt
import json
import random
import re
import signal
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

STATE_FILE = Path.home() / ".poe2_macro_state.json"

# ---------------- config ----------------
TOGGLE_VK = 0x34            # '4' — hotkey fires only with Shift held.
QUIT_VK = 0x35              # '5' — hotkey fires only with Shift held.
VK_SHIFT = 0x10

# Auto-pause: when the foreground window is neither the game nor this overlay,
# the macro is forced OFF and the overlay is hidden. We identify the game by its
# executable name (not its window title) — a browser on the PoE trade site has
# "Path of Exile" in its title too and would otherwise falsely match.
GAME_EXE_MATCH = "pathofexile"
FOCUS_POLL_MS = 250

# Optional zone gate: when "pause outside maps" is on, the macro only fires while
# you're in a map/combat zone, staying paused in hideout/town. The current zone
# is read from the game's Client.txt log.
LOG_POLL_MS = 700

# Fallback Client.txt locations if we can't derive it from the game process
# (e.g. the game runs elevated and we can't query its path).
LOG_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile 2\logs\Client.txt",
    r"C:\Program Files\Grinding Gear Games\Path of Exile 2\logs\Client.txt",
    r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile 2\logs\Client.txt",
]

# Don't fire into text fields. The in-game Currency Exchange / market streams
# "[ETrade]" lines to the log while open, and pressing Enter opens chat — in
# both cases we suppress the macro's keystrokes for a short window so it never
# types into a search/chat box, without the user toggling anything.
MARKET_SUPPRESS_SECONDS = 12  # after the last market (ETrade) log activity
CHAT_SUPPRESS_SECONDS = 5     # after the user presses Enter (chat)
VK_RETURN = 0x0D

# Reactive follow-up: while the macro is ON, a real E press triggers F to be
# pressed after gauss(TRIGGER_DELAY_MEAN, TRIGGER_DELAY_SIGMA) seconds.
TRIGGER_VK = 0x45           # 'E' — the key the user presses
TRIGGER_TARGET_VK = 0x46    # 'F' — the key the macro fires in response
TRIGGER_DELAY_MEAN = 0.49   # seconds
TRIGGER_DELAY_SIGMA = 0.01  # seconds (jitter kept inside the clamp window below)
TRIGGER_DELAY_MIN = 0.47    # seconds (hard floor — never fire F sooner than this)
TRIGGER_DELAY_MAX = 0.51    # seconds (hard ceiling)
# Per-key timing. Each key runs in its own thread; the slider mutates "mean"
# live so changes take effect on the next press. σ is auto-derived as a fixed
# fraction of the mean, so longer intervals get proportionally wider jitter.
KEY_CFG = [
    {"vk": 0x31, "label": "1", "mean": 0.50},
]
SIGMA_PCT = 0.10            # σ as fraction of mean (10% → ~95% within ±20%)
MEAN_MIN = 0.1              # slider lower bound (s)
MEAN_MAX = 10.0             # slider upper bound (s)
HOLD_MS = 22                # how long each key is held down


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(d):
    try:
        STATE_FILE.write_text(json.dumps(d))
    except Exception:
        pass


_saved = load_state()
# Apply persisted means before the sliders are built. Saved means are keyed
# by hex VK so reorderings in KEY_CFG don't scramble them.
_saved_means = _saved.get("means", {})
for _cfg in KEY_CFG:
    _persisted = _saved_means.get(hex(_cfg["vk"]))
    if isinstance(_persisted, (int, float)):
        _cfg["mean"] = max(MEAN_MIN, min(MEAN_MAX, float(_persisted)))

# ---------------- Win32 types ----------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
LPARAM = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class KBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD),
                ("wParamH", wt.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.MapVirtualKeyW.restype = wt.UINT
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def send_key(vk):
    """Press and release a key via SendInput, using scancodes for game compat."""
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    if scan:
        flags_dn = KEYEVENTF_SCANCODE
        wv = 0
    else:
        flags_dn = 0
        wv = vk
    flags_up = flags_dn | KEYEVENTF_KEYUP
    down = INPUT(type=INPUT_KEYBOARD)
    down.ki = KBDINPUT(wv, scan, flags_dn, 0, 0)
    up = INPUT(type=INPUT_KEYBOARD)
    up.ki = KBDINPUT(wv, scan, flags_up, 0, 0)
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(HOLD_MS / 1000.0)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


# ---------------- low-level keyboard hook ----------------
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc,
                                     ctypes.c_void_p, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p,
                               wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, WPARAM, LPARAM]
user32.PostThreadMessageW.restype = wt.BOOL


# ---------------- DPI awareness (crisp tk text on 125% scaling) ----------------
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def system_dpi():
    try:
        return user32.GetDpiForSystem()
    except Exception:
        return 96


enable_dpi_awareness()
DPI = system_dpi()

# ---------------- macro state ----------------
active = False
stop_event = threading.Event()
hook_thread_id = 0
_hook_cb_ref = None  # keep WINFUNCTYPE callback alive

# Zone gate state
pause_outside_maps = bool(_saved.get("pause_outside_maps", False))
current_area = ""         # internal area id from Client.txt (e.g. "HideoutFelled")
current_zone = "unknown"  # "map" | "hideout" | "town" | "unknown"
_client_log_path = None   # resolved from the game's executable path
# Manual override: a hotkey toggle wins over the zone auto-state until the next
# zone change, so you can force the macro off in a map (or on in town) and have
# it stick instead of being snapped back on the next focus poll.
_zone_override = False

# Text-field guards (monotonic timestamps of last activity)
_last_market_ts = 0.0     # last "[ETrade]" market log line
_last_enter_ts = 0.0      # last real Enter press (chat)
_game_focused = True      # is the game (or the overlay) the foreground window


def in_combat_zone():
    return current_zone == "map"


def market_active():
    return (time.monotonic() - _last_market_ts) < MARKET_SUPPRESS_SECONDS


def chat_typing():
    return (time.monotonic() - _last_enter_ts) < CHAT_SUPPRESS_SECONDS


def input_blocked():
    """A text field (market search or chat) is probably focused."""
    return market_active() or chat_typing()


def sending_allowed():
    """Whether the macro should actually send keys right now: ON, not stopping,
    in a combat zone (if that gate is on), and not while a text field is open."""
    if not active or stop_event.is_set():
        return False
    if pause_outside_maps and not in_combat_zone() and not _zone_override:
        return False
    if input_blocked():
        return False
    return True


def apply_zone_autostate():
    """When auto-by-zone is enabled, the macro state is driven entirely by the
    zone: ON in a map (while the game is focused), OFF in hideout/town or when
    not focused. Enforced continuously so it can't get stuck on."""
    global active
    if not pause_outside_maps:
        return
    if _zone_override:  # a manual toggle is in effect; leave it until zone change
        return
    desired = in_combat_zone() and _game_focused
    if active != desired:
        active = desired
        print(f"[macro] auto-{'ON' if desired else 'OFF'} (zone: {current_zone})",
              flush=True)
        _safe_refresh()


def schedule_trigger_followup():
    """Press TRIGGER_TARGET_VK after gauss(MEAN, SIGMA)s. Re-checks state before
    firing so toggling off (or leaving a map) during the wait cancels it."""
    if not sending_allowed():
        return
    delay = max(TRIGGER_DELAY_MIN,
                min(TRIGGER_DELAY_MAX,
                    random.gauss(TRIGGER_DELAY_MEAN, TRIGGER_DELAY_SIGMA)))

    def _fire():
        if sending_allowed():
            send_key(TRIGGER_TARGET_VK)

    t = threading.Timer(delay, _fire)
    t.daemon = True
    t.start()


def spam_loop(cfg):
    """One key, one thread. Reads mean live from cfg each iteration; σ tracks it."""
    # Stagger initial delay so the keys don't start in lockstep.
    time.sleep(random.uniform(0, cfg["mean"]))
    while not stop_event.is_set():
        if not sending_allowed():
            time.sleep(0.05)
            continue
        m = cfg["mean"]
        s = m * SIGMA_PCT
        lo = max(0.05, m - 4 * s)
        hi = max(lo, m + 4 * s)
        delay = max(lo, min(hi, random.gauss(m, s) if s > 0 else m))
        end = time.time() + delay
        while time.time() < end:
            if stop_event.is_set() or not sending_allowed():
                break
            time.sleep(0.02)
        if sending_allowed():
            send_key(cfg["vk"])


# ---------------- overlay ----------------
TRANSP = "#010101"
BG = TRANSP
BORDER = TRANSP
BORDER_ON = "#e8a500"
FG_ON = "#21d65a"
FG_OFF = "#888888"
FG_DIM = "#666"

root = tk.Tk()
root.title("PoE2 Macro")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.attributes("-alpha", 0.92)
root.attributes("-transparentcolor", TRANSP)
root.configure(bg=BG)

# Match tk's point sizing to the real monitor DPI so text isn't bitmap-upscaled.
try:
    root.tk.call("tk", "scaling", DPI / 72)
except tk.TclError:
    pass

frame = tk.Frame(
    root, bg=BG, bd=0, highlightthickness=2,
    highlightbackground=BORDER, highlightcolor=BORDER,
)
frame.pack(fill="both", expand=True)

state_lbl = tk.Label(
    frame, text="MACRO  OFF", bg=BG, fg=FG_OFF,
    font=("Segoe UI", 14, "bold"), padx=14, pady=2,
)
state_lbl.pack(fill="x", pady=(6, 0))

hint_lbl = tk.Label(
    frame, text="⇧4 toggle    ⇧5 quit    click to adjust · drag to move",
    bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=10, pady=2,
)
hint_lbl.pack(fill="x", pady=(0, 2))

zone_lbl = tk.Label(
    frame, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=10, pady=0,
)
zone_lbl.pack(fill="x", pady=(0, 6))

# ----- settings panel (sliders) -----
PANEL_BG = "#161616"
SCALE_TROUGH = "#2a2a2a"
SCALE_FG = "#cccccc"

settings_panel = tk.Frame(frame, bg=PANEL_BG)
# not packed until the user clicks to expand

tk.Label(
    settings_panel,
    text=f"key      interval (s)   (σ auto = {int(SIGMA_PCT * 100)}% of interval)",
    bg=PANEL_BG, fg=FG_DIM, font=("Segoe UI", 7), anchor="w",
).pack(fill="x", padx=8, pady=(4, 0))

for _cfg in KEY_CFG:
    row = tk.Frame(settings_panel, bg=PANEL_BG)
    row.pack(fill="x", padx=8, pady=1)

    tk.Label(
        row, text=_cfg["label"], width=2, bg=PANEL_BG, fg="#dddddd",
        font=("Segoe UI", 10, "bold"),
    ).pack(side="left")

    def _make_mean_cb(c):
        def _cb(v):
            c["mean"] = float(v)
            persist()
        return _cb

    mean_scale = tk.Scale(
        row, from_=MEAN_MIN, to=MEAN_MAX, resolution=0.05, orient="horizontal",
        bg=PANEL_BG, fg=SCALE_FG, troughcolor=SCALE_TROUGH,
        highlightthickness=0, bd=0, length=240, sliderlength=14,
        font=("Segoe UI", 7), command=_make_mean_cb(_cfg),
    )
    mean_scale.set(_cfg["mean"])
    mean_scale.pack(side="left", padx=(4, 4))

pom_var = tk.BooleanVar(value=pause_outside_maps)


def _toggle_pom():
    global pause_outside_maps
    pause_outside_maps = bool(pom_var.get())
    persist()
    apply_zone_autostate()  # take effect immediately for the current zone
    refresh()


tk.Checkbutton(
    settings_panel, text="Auto-run in maps (on in maps, off in hideout/town)",
    variable=pom_var, command=_toggle_pom,
    bg=PANEL_BG, fg="#dddddd", selectcolor="#333", activebackground=PANEL_BG,
    activeforeground="#ffffff", font=("Segoe UI", 8), anchor="w",
    bd=0, highlightthickness=0,
).pack(fill="x", padx=8, pady=(2, 0))

tk.Frame(settings_panel, bg=PANEL_BG, height=4).pack(fill="x")

settings_open = False


def fit_window():
    root.update_idletasks()
    root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")


def toggle_settings():
    global settings_open
    settings_open = not settings_open
    if settings_open:
        settings_panel.pack(fill="x", padx=4, pady=(0, 4))
    else:
        settings_panel.pack_forget()
    fit_window()


# Position the window after laying out the compact view. Restore saved
# position if we have one.
fit_window()
_pos = _saved.get("pos", [220, 220])
root.geometry(f"+{int(_pos[0])}+{int(_pos[1])}")


def persist():
    try:
        save_state({
            "means": {hex(c["vk"]): c["mean"] for c in KEY_CFG},
            "pos":   [root.winfo_x(), root.winfo_y()],
            "pause_outside_maps": pause_outside_maps,
        })
    except tk.TclError:
        pass


def refresh():
    if active:
        if input_blocked():
            why = "market" if market_active() else "chat"
            state_lbl.config(text=f"MACRO  HELD ({why})", fg=FG_OFF)
        elif pause_outside_maps and not in_combat_zone() and not _zone_override:
            state_lbl.config(text="MACRO  PAUSED", fg=FG_OFF)
        else:
            state_lbl.config(text="MACRO  ON", fg=FG_ON)
    else:
        state_lbl.config(text="MACRO  OFF", fg=FG_OFF)
    zone_lbl.config(
        text=(f"zone: {current_zone}" if current_zone != "unknown" else "")
    )


def toggle_macro():
    global active, _zone_override
    active = not active
    # A manual toggle overrides the zone auto-state until the next zone switch.
    _zone_override = pause_outside_maps
    refresh()
    print(f"[macro] {'ON' if active else 'OFF'}"
          f"{' (manual override)' if _zone_override else ''}", flush=True)


overlay_visible = True   # tracks shown/hidden so we only call withdraw/deiconify on change
_resume_on_return = False  # was the macro ON when we auto-paused? restore it on return


def foreground_title():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def foreground_exe():
    """Full path of the foreground window's process executable ('' on failure)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return ""


GEN_RE = re.compile(r'Generating level \d+ area "([^"]+)" with seed (\d+)')


def classify_area(area_id, seed):
    """Map an internal area id + seed to a coarse zone type. Hideouts and town
    hubs are static; maps and campaign zones are the combat areas we spam in."""
    a = area_id.lower()
    if "simulacrum" in a:  # Simulacrum is a combat encounter -> always "on"
        return "map"
    if "hideout" in a:
        return "hideout"
    if "hub" in a or "town" in a:
        return "town"
    if seed != 1:  # generated instance -> combat zone (map or campaign area)
        return "map"
    return "town"


def log_watch():
    """Tail the game's Client.txt and track the current zone from the
    'Generating level N area "X" with seed S' lines it writes on every entry."""
    global current_area, current_zone, _last_market_ts, _zone_override
    f = None
    while not stop_event.is_set():
        if _client_log_path is None:
            time.sleep(LOG_POLL_MS / 1000.0)
            continue
        if f is None:
            try:
                f = open(_client_log_path, "r", encoding="utf-8", errors="ignore")
            except OSError:
                time.sleep(LOG_POLL_MS / 1000.0)
                continue
            # Initialise from the tail of the existing log, then follow new lines.
            try:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200000))
                matches = list(GEN_RE.finditer(f.read()))
                if matches:
                    area, seed = matches[-1].group(1), int(matches[-1].group(2))
                    current_area, current_zone = area, classify_area(area, seed)
                    print(f"[macro] zone: {current_zone} ({area})", flush=True)
                    apply_zone_autostate()
                    _safe_refresh()
            except (OSError, ValueError):
                pass
            f.seek(0, 2)
        line = f.readline()
        if not line:
            time.sleep(LOG_POLL_MS / 1000.0)
            continue
        if "[ETrade]" in line:  # in-game market open/active -> suppress typing
            _last_market_ts = time.monotonic()
        m = GEN_RE.search(line)
        if m:
            area, seed = m.group(1), int(m.group(2))
            if area != current_area:  # genuinely changed zones -> auto-state resumes
                _zone_override = False
            current_area, current_zone = area, classify_area(area, seed)
            print(f"[macro] zone: {current_zone} ({area})", flush=True)
            apply_zone_autostate()
            _safe_refresh()


def _safe_refresh():
    try:
        root.after(0, refresh)
    except tk.TclError:
        pass


def watch_focus():
    """Poll the foreground window. When the user swaps away from the game, hide
    the overlay and pause the macro, remembering whether it was ON. When the
    game comes back, restore the overlay and re-enable the macro if it had been
    ON before the swap."""
    global overlay_visible, active, _resume_on_return, _client_log_path, _game_focused
    if stop_event.is_set():
        return
    title = foreground_title()
    exe = foreground_exe()
    exe_base = exe.rsplit("\\", 1)[-1].lower() if exe else ""
    if exe_base:
        on_game = GAME_EXE_MATCH in exe_base
    else:
        # Couldn't read the process (e.g. game elevated, overlay not). Fall back
        # to an exact title match — still excludes browser tabs that merely
        # contain "Path of Exile" alongside the site name and browser name.
        on_game = title.strip().lower() in ("path of exile 2", "path of exile")
    on_self = title == root.title()
    _game_focused = on_game or on_self

    # Resolve the game's Client.txt for the zone watcher (process path first,
    # then known install locations).
    if _client_log_path is None:
        if on_game and exe:
            cand = Path(exe).parent / "logs" / "Client.txt"
            if cand.exists():
                _client_log_path = str(cand)
        if _client_log_path is None:
            for c in LOG_CANDIDATES:
                if Path(c).exists():
                    _client_log_path = c
                    break
    if on_game or on_self:
        if not overlay_visible:  # just tabbed back in
            root.deiconify()
            root.attributes("-topmost", True)
            overlay_visible = True
            if _resume_on_return:
                _resume_on_return = False
                active = True
                refresh()
                print("[macro] auto-ON (focus returned)", flush=True)
    else:
        if overlay_visible:  # just tabbed away — remember state, then pause
            _resume_on_return = active
            root.withdraw()
            overlay_visible = False
        if active:
            active = False
            refresh()
            print("[macro] auto-OFF (focus left game)", flush=True)
    apply_zone_autostate()  # keep macro state reconciled with the current zone
    refresh()  # keep the HELD/zone indicator live (time-based suppression)
    root.after(FOCUS_POLL_MS, watch_focus)


def quit_app(*_):
    if stop_event.is_set():
        return
    stop_event.set()
    persist()
    print("[macro] quitting", flush=True)
    if hook_thread_id:
        try:
            user32.PostThreadMessageW(hook_thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
    try:
        root.destroy()
    except tk.TclError:
        pass


# Click = toggle settings, drag = move window.
# A short press without movement counts as a click; once the mouse moves >4px
# while held, we lock into drag mode for the rest of that gesture.
drag_data = {"x": 0, "y": 0, "down_x": 0, "down_y": 0, "moved": False}
DRAG_THRESHOLD = 4


def on_press(e):
    drag_data["x"] = e.x_root - root.winfo_x()
    drag_data["y"] = e.y_root - root.winfo_y()
    drag_data["down_x"] = e.x_root
    drag_data["down_y"] = e.y_root
    drag_data["moved"] = False


def on_motion(e):
    if not drag_data["moved"]:
        dx = abs(e.x_root - drag_data["down_x"])
        dy = abs(e.y_root - drag_data["down_y"])
        if dx > DRAG_THRESHOLD or dy > DRAG_THRESHOLD:
            drag_data["moved"] = True
    if drag_data["moved"]:
        root.geometry(f"+{e.x_root - drag_data['x']}+{e.y_root - drag_data['y']}")


def on_release(_e):
    if drag_data["moved"]:
        persist()
    else:
        toggle_settings()


# Bind on the header widgets only. Do NOT bind on root: tk fires toplevel
# bindings in addition to widget bindings, which would double-trigger
# toggle_settings on every click (open → close, looking like a flicker).
# Scale widgets inside settings_panel have their own class bindings and won't
# reach these handlers.
for w_ in (frame, state_lbl, hint_lbl):
    w_.bind("<Button-1>", on_press)
    w_.bind("<B1-Motion>", on_motion)
    w_.bind("<ButtonRelease-1>", on_release)

root.bind("<Escape>", quit_app)


# ---------------- hook thread ----------------
def hook_thread_main():
    global hook_thread_id, _hook_cb_ref
    hook_thread_id = kernel32.GetCurrentThreadId()

    def proc(nCode, wParam, lParam):
        # When a hotkey combo is consumed we return 1 instead of chaining the
        # hook, which swallows the keystroke so Shift+4 / Shift+5 never reach
        # the focused app (no '$' / '%' typed).
        global _last_enter_ts
        suppress = False
        try:
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT))[0]
                injected = kbd.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)
                if not injected:
                    vk = kbd.vkCode
                    shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                    if vk == VK_RETURN:
                        # Enter opens chat — hold the macro briefly so it doesn't
                        # type into the chat box. (Never suppress Enter itself.)
                        _last_enter_ts = time.monotonic()
                    if vk == TOGGLE_VK and shift:
                        root.after(0, toggle_macro)
                        suppress = True
                    elif vk == QUIT_VK and shift:
                        root.after(0, quit_app)
                        suppress = True
                    elif vk == TRIGGER_VK and active:
                        schedule_trigger_followup()
        except Exception as e:
            print(f"[macro] hook proc error: {e}", file=sys.stderr, flush=True)
        if suppress:
            return 1
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    _hook_cb_ref = LowLevelKeyboardProc(proc)
    h = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_cb_ref, None, 0)
    if not h:
        err = ctypes.GetLastError()
        print(f"[macro] SetWindowsHookExW failed (err={err}) -- "
              "toggle/quit hotkeys will not work.", file=sys.stderr, flush=True)
        return
    print("[macro] hook installed. press Shift+4 to toggle, Shift+5 to quit.",
          flush=True)
    msg = wt.MSG()
    while not stop_event.is_set():
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r in (0, -1):
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.UnhookWindowsHookEx(h)


threading.Thread(target=hook_thread_main, daemon=True).start()
threading.Thread(target=log_watch, daemon=True).start()
for _cfg in KEY_CFG:
    threading.Thread(target=spam_loop, args=(_cfg,), daemon=True).start()


# ---------------- Ctrl+C from terminal ----------------
signal.signal(signal.SIGINT, lambda *_: quit_app())
# Periodic tick: tk's mainloop blocks signal delivery on Windows unless the
# interpreter periodically gets control back. This wakeup makes Ctrl+C work.
def _tick():
    if not stop_event.is_set():
        root.after(100, _tick)
root.after(100, _tick)
root.after(FOCUS_POLL_MS, watch_focus)


refresh()
try:
    root.mainloop()
finally:
    stop_event.set()
    if hook_thread_id:
        try:
            user32.PostThreadMessageW(hook_thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
