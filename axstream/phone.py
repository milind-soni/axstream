"""iPhone control through the macOS iPhone Mirroring window.

The mirror is just a window: `screencapture` + Vision OCR for eyes, driver
events for hands. Two facts shape everything here (both measured, both
inherited from the phone-harness project this absorbs):

- There is NO accessibility inside the window — it is a video stream. The
  click ladder's AX rung can never resolve; targets must be OCR text or
  pixels, and OCR is the ground truth for state.
- The mirror swallows input that arrives while it is not frontmost, and it
  ignores the unicode payload of type_text events — typing must travel as raw
  keycodes (see `type_text`), and every input burst fronts the window first.

Connection is the USER'S job. Reconnecting mirroring is a physical action
(open the app, approve prompts, LOCK the phone when it says "iPhone in Use").
`ensure_ready()` gates every entry point: when the session isn't ready it
raises PhoneNotReady with instructions to relay — never tap Connect, never
poll for reconnection.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .driver import DriverComputer, WindowSnapshot
from . import ocr as _ocr

APP_NAME = "iPhone Mirroring"

# Distinctive strings on the not-connected interstitials. Any of these visible
# means a human has to act; the agent must stop and say so.
_BLOCKED_MARKERS = ("iphone in use", "lock your iphone", "mirroring ended",
                    "to connect")

# The mirrored screen's chrome, as window-height fractions: status bar
# (clock/battery — volatile, breaks settle detection) and the home-indicator
# strip. Content scans exclude both.
_TOP_CHROME_FRAC = 0.06
_BOTTOM_CHROME_FRAC = 0.92


class PhoneNotReady(RuntimeError):
    """Raised when the mirroring session needs the user. `.state` is one of
    'not-running' | 'no-window' | 'blocked'; the message is user-relayable."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


_STATE_MESSAGES = {
    "not-running": (
        "iPhone Mirroring isn't running. Ask the user to open the iPhone "
        "Mirroring app and connect their phone — reconnecting is physical, "
        "the agent can't do it."),
    "no-window": (
        "iPhone Mirroring is open but no phone window exists. Ask the user "
        "to connect their phone in the app."),
    "blocked": (
        "iPhone Mirroring is showing a connect / 'iPhone in Use' screen. "
        "This needs the user: open iPhone Mirroring, and if it says 'iPhone "
        "in Use', LOCK the iPhone so mirroring can resume. Never tap "
        "Connect for them."),
}


async def state(computer: DriverComputer) -> dict:
    """{'state': 'ready'|'blocked'|'no-window'|'not-running',
        'snapshot': WindowSnapshot|None, 'instructions': str|None}

    Cheap preflight — call before acting, relay `instructions` on non-ready.
    """
    windows = await _mirror_windows(computer)
    if windows is None:
        return {"state": "not-running", "snapshot": None,
                "instructions": _STATE_MESSAGES["not-running"]}
    if not windows:
        return {"state": "no-window", "snapshot": None,
                "instructions": _STATE_MESSAGES["no-window"]}
    snap = await _target_mirror(computer, fresh_shot=True)
    if snap is None:
        return {"state": "no-window", "snapshot": None,
                "instructions": _STATE_MESSAGES["no-window"]}
    if snap.shot_path:
        texts = " ".join(h.text for h in _ocr.all_text(snap.shot_path)).lower()
        if any(marker in texts for marker in _BLOCKED_MARKERS):
            return {"state": "blocked", "snapshot": snap,
                    "instructions": _STATE_MESSAGES["blocked"]}
    return {"state": "ready", "snapshot": snap, "instructions": None}


async def ensure_ready(computer: DriverComputer) -> WindowSnapshot:
    """Preflight + front the mirror (it swallows non-frontmost input).
    Returns the window snapshot, or raises PhoneNotReady with relayable
    instructions. Never launches the app or taps through interstitials."""
    s = await state(computer)
    if s["state"] != "ready":
        raise PhoneNotReady(s["state"], s["instructions"])
    await computer.tool("bring_to_front", pid=s["snapshot"].pid)
    await asyncio.sleep(0.25)
    return s["snapshot"]


def _app_running() -> bool:
    """Is iPhone Mirroring running at all? (list_windows can't distinguish
    'running but windowless' from 'not running'.) Soft-true when AppKit is
    unavailable so the preflight degrades to no-window rather than lying."""
    try:
        from AppKit import NSRunningApplication
    except Exception:
        return True
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        "com.apple.ScreenContinuity")
    return bool(apps)


async def _mirror_windows(computer: DriverComputer) -> Optional[list]:
    """Usable mirror windows; None when the app isn't running at all."""
    if not _app_running():
        return None
    res = await computer.tool("list_windows")
    mine = [w for w in res.get("windows") or []
            if (w.get("app_name") or "") == APP_NAME]
    # Panels/toolbars are not the phone; the phone window is portrait-tall.
    return [w for w in mine
            if (w.get("bounds") or {}).get("width", 0) >= 100]


async def _target_mirror(computer: DriverComputer,
                         fresh_shot: bool = False) -> Optional[WindowSnapshot]:
    """Point the computer at the mirror app and return its window snapshot."""
    windows = await _mirror_windows(computer)
    if not windows:
        return None
    pid = next((int(w["pid"]) for w in windows if w.get("pid")), None)
    if pid is None:
        return None
    computer.target_pid = pid
    return await computer.window_geometry(fresh_shot=fresh_shot)


# --- typing: raw keycodes only ---
#
# The mirror forwards HID keycodes to iOS and drops the unicode payload the
# daemon's type_text attaches (keycode 0 + set_string). So phone typing walks
# the text and presses real keys — slower than a text event, but it ARRIVES.

_SHIFTED = {
    "A": "a", "B": "b", "C": "c", "D": "d", "E": "e", "F": "f", "G": "g",
    "H": "h", "I": "i", "J": "j", "K": "k", "L": "l", "M": "m", "N": "n",
    "O": "o", "P": "p", "Q": "q", "R": "r", "S": "s", "T": "t", "U": "u",
    "V": "v", "W": "w", "X": "x", "Y": "y", "Z": "z",
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", ":": ";", '"': "'",
    "<": ",", ">": ".", "?": "/", "~": "`", "{": "[", "}": "]", "|": "\\",
}

# Character -> daemon key name (keyboard.rs key_name_to_code vocabulary).
_KEY_NAMES = {
    " ": "space", ".": "period", ",": "comma", "/": "slash",
    ";": "semicolon", "'": "quote", "[": "leftbracket", "]": "rightbracket",
    "\\": "backslash", "-": "minus", "=": "equal", "`": "grave",
}


async def type_text(computer: DriverComputer, text: str,
                    delay: float = 0.02) -> None:
    """Type into the focused iOS field via per-key presses (US layout).
    Newlines press return. Raises on characters with no keycode (emoji)."""
    await ensure_ready(computer)
    for i, line in enumerate(text.split("\n")):
        if i:
            await computer.key(["return"])
        for ch in line:
            shifted = ch in _SHIFTED
            base = _SHIFTED.get(ch, ch)
            key = _KEY_NAMES.get(base, base)
            if len(key) == 1 and not (key.isalnum() and key.isascii()):
                raise ValueError(f"cannot type {ch!r} via keycodes")
            if shifted:
                await computer.key(["shift", key])
            else:
                await computer.key([key])
            if delay:
                await asyncio.sleep(delay)


# --- gestures ---

async def tap_text(computer: DriverComputer, query: str,
                   index: int = 0, exact: bool = False) -> dict:
    """OCR the mirror and tap the matching text. Raises with what IS visible
    on a miss, so the caller's next step is informed."""
    snap = await ensure_ready(computer)
    snap = await computer.window_geometry(fresh_shot=True) or snap
    hits = _visible_text(snap)
    q = query.lower()
    matches = [h for h in hits
               if (h.text.lower() == q if exact else q in h.text.lower())]
    if not matches:
        visible = [h.text for h in hits][:30]
        raise RuntimeError(f"no visible text matches {query!r}; saw: {visible}")
    hit = matches[index]
    await computer.click_window_pixel(snap.pid, snap.window_id, hit.x, hit.y)
    return {"text": hit.text, "x": hit.x, "y": hit.y}


async def swipe(computer: DriverComputer, direction: str,
                distance: float = 0.4) -> None:
    """Momentum flick centered in the phone window. Direction is finger
    motion: swipe('up') moves content up. Use scroll helpers for lists —
    a flick is for pages/carousels."""
    snap = await ensure_ready(computer)
    size = snap.screenshot_size or (0, 0)
    if not size[0]:
        raise RuntimeError("mirror window has no screenshot dimensions")
    cx, cy = size[0] / 2, size[1] / 2
    dx = {"left": -1, "right": 1}.get(direction, 0) * size[0] * distance
    dy = {"up": -1, "down": 1}.get(direction, 0) * size[1] * distance
    if not dx and not dy:
        raise ValueError(f"unknown direction {direction!r}")
    w, h = (snap.bounds.get("width") or 1), (snap.bounds.get("height") or 1)
    frag = {"w": w, "h": h}
    await computer.drag(
        {"win": {"fx": (cx - dx / 2) / size[0], "fy": (cy - dy / 2) / size[1], **frag}},
        {"win": {"fx": (cx + dx / 2) / size[0], "fy": (cy + dy / 2) / size[1], **frag}})


def _visible_text(snap: WindowSnapshot) -> list:
    if not snap.shot_path:
        return []
    return _ocr.all_text(snap.shot_path)


def content_text(snap: WindowSnapshot, min_confidence: float = 0.4) -> list:
    """OCR hits inside the scrollable content area — status bar and home
    strip excluded (their clock/battery churn breaks movement detection)."""
    size = snap.screenshot_size
    if not size or not snap.shot_path:
        return []
    top = size[1] * _TOP_CHROME_FRAC
    bottom = size[1] * _BOTTOM_CHROME_FRAC
    return [h for h in _ocr.all_text(snap.shot_path)
            if top < h.y < bottom and h.confidence >= min_confidence]


# --- navigation (iPhone Mirroring's own shortcuts) ---

async def home(computer: DriverComputer) -> None:
    await ensure_ready(computer)
    await computer.key(["cmd", "1"])
    await asyncio.sleep(0.8)


async def app_switcher(computer: DriverComputer) -> None:
    await ensure_ready(computer)
    await computer.key(["cmd", "2"])
    await asyncio.sleep(0.8)


async def open_app(computer: DriverComputer, name: str) -> None:
    """Open an app via the mirror's Spotlight (Cmd+3)."""
    await ensure_ready(computer)
    await computer.key(["cmd", "3"])
    await asyncio.sleep(0.9)
    await type_text(computer, name)
    await asyncio.sleep(1.2)  # let results populate before committing
    await computer.key(["return"])
