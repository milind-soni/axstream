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
import time as _time
from typing import Optional

from .driver import DriverComputer, DriverError, WindowSnapshot
from . import ocr as _ocr

APP_NAME = "iPhone Mirroring"
_BUNDLE_ID = "com.apple.ScreenContinuity"

# Activation is ASYNCHRONOUS: the grant + focus transition takes up to ~1.5s,
# and HID events posted mid-transition are swallowed silently (measured live:
# cmd+3 at 0.5s after activation never arrived; at 1.5s it opened Spotlight).
# It CANNOT be confirmed by polling — NSWorkspace focus reads freeze at their
# first value in a runloop-less process (pumping the runloop does not help),
# so a "wait until frontmost" loop starves on stale data and refuses falsely.
# Hence: activate blind, settle the measured time, then TRUST the result for
# a short window so a burst of steps pays one settle, not one per keystroke.
_FRONT_SETTLE_S = 1.5
_FRONT_TRUST_S = 2.0

# A ready verdict stays trusted this long. Without it every phone step pays
# the full preflight — two list_windows round-trips, a fresh screenshot, and
# a whole-window OCR pass — which dominates an otherwise instant tap. Within
# the TTL the window's existence and bounds are still re-read live (cheap),
# and keyboard focus is still re-asserted (an in-process NSWorkspace check;
# the _FRONT_SETTLE_S cost is paid only when focus actually moved).
_READY_TTL_S = 2.0

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
        'snapshot': WindowSnapshot|None, 'instructions': str|None,
        'ocr_available': bool}

    Cheap preflight — call before acting, relay `instructions` on non-ready.
    `ocr_available` False means the blocked-interstitial scan is blind (no
    pyobjc): the session may look ready while showing a Connect screen.
    """
    windows = await _mirror_windows(computer)
    if windows is None:
        return {"state": "not-running", "snapshot": None,
                "instructions": _STATE_MESSAGES["not-running"],
                "ocr_available": _ocr.available()}
    if not windows:
        return {"state": "no-window", "snapshot": None,
                "instructions": _STATE_MESSAGES["no-window"],
                "ocr_available": _ocr.available()}
    snap = await _target_mirror(computer, fresh_shot=True, windows=windows)
    if snap is None:
        return {"state": "no-window", "snapshot": None,
                "instructions": _STATE_MESSAGES["no-window"],
                "ocr_available": _ocr.available()}
    if snap.shot_path and _ocr.available():
        texts = " ".join(h.text for h in _ocr.all_text(snap.shot_path)).lower()
        if any(marker in texts for marker in _BLOCKED_MARKERS):
            return {"state": "blocked", "snapshot": snap,
                    "instructions": _STATE_MESSAGES["blocked"],
                    "ocr_available": True}
    return {"state": "ready", "snapshot": snap, "instructions": None,
            "ocr_available": _ocr.available()}


def _truly_frontmost() -> Optional[bool]:
    """Does the mirror hold KEYBOARD FOCUS, per NSWorkspace — the authority
    the HID tap obeys. None when AppKit is unavailable (caller falls back to
    the driver's z-order signal). In-process and effectively free."""
    try:
        from AppKit import NSWorkspace
    except Exception:
        return None
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return bool(front and front.bundleIdentifier() == _BUNDLE_ID)


def _activate_mirror() -> bool:
    """App-level activation (NSApplicationActivateIgnoringOtherApps) — the
    only reliable way to MOVE keyboard focus. Returns False when AppKit or
    the app is unavailable so the caller can fall back to the driver."""
    try:
        from AppKit import NSRunningApplication
    except Exception:
        return False
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        _BUNDLE_ID)
    if not apps:
        return False
    apps[0].activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
    return True


async def _front(computer: DriverComputer, pid: int) -> None:
    """Give the mirror KEYBOARD FOCUS — z-order is not focus. The driver can
    report the mirror top-of-stack (its window floats high in the stack)
    while the terminal that launched us keeps keyboard focus, and the mirror
    silently swallows every HID event that arrives unfocused — the
    typed-into-nothing failure. So z-order never vetoes fronting here.

    Ladder (each rung's behavior measured live on-device):
    1. Recently fronted by us -> trust it for _FRONT_TRUST_S: a burst pays
       one settle, not one per step.
    2. NSWorkspace says the mirror is focused -> done. This read is truthful
       at least once per process (fresh at first use; later reads can be
       stale, which only costs an unnecessary re-activation — the stale
       value is the PRE-activation state, never a false 'focused').
    3. Activate app-level (NSRunningApplication) and settle the measured
       transition time. Activation is granted to this background process
       but takes up to ~1.5s; there is no reliable way to poll for the flip
       (see _FRONT_SETTLE_S), so the settle is blind by design.
    The driver's bring_to_front is only the AppKit-less fallback — it raises
    the window without reliably moving keyboard focus."""
    now = _time.monotonic()
    if now < getattr(computer, "_front_trusted_until", 0):
        return
    if _truly_frontmost():
        computer._front_trusted_until = now + _FRONT_TRUST_S
        return
    if not _activate_mirror():
        try:
            await computer.tool("bring_to_front", pid=pid)
        except DriverError as e:
            raise PhoneNotReady(
                "no-window",
                f"could not front the iPhone Mirroring window ({e}). Ask the "
                "user to click the iPhone Mirroring window — if it's gone, "
                "reconnect the phone in the app.") from e
    await asyncio.sleep(_FRONT_SETTLE_S)
    computer._front_trusted_until = _time.monotonic() + _FRONT_TRUST_S


async def ensure_ready(computer: DriverComputer) -> WindowSnapshot:
    """Preflight + front the mirror (it swallows non-frontmost input).
    Returns the window snapshot, or raises PhoneNotReady with relayable
    instructions. Never launches the app or taps through interstitials.

    A ready verdict is cached for _READY_TTL_S on the computer instance:
    the fast path re-reads the window live (existence + bounds) but skips
    the screenshot+OCR interstitial scan and the re-fronting settle."""
    cached = getattr(computer, "_phone_ready", None)
    if cached is not None and _time.monotonic() - cached[0] < _READY_TTL_S:
        pid = cached[1]
        computer.target_pid = pid
        await _front(computer, pid)
        # the base implementation, even on a PhoneComputer — its override
        # routes straight back here
        snap = await DriverComputer.window_geometry(computer)
        if snap is not None:
            return snap
        # the window vanished mid-TTL — fall through to the full preflight
    s = await state(computer)
    if s["state"] != "ready":
        computer._phone_ready = None
        raise PhoneNotReady(s["state"], s["instructions"] or s["state"])
    pid = s["snapshot"].pid
    await _front(computer, pid)
    computer._phone_ready = (_time.monotonic(), pid)
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
                         fresh_shot: bool = False,
                         windows: Optional[list] = None) -> Optional[WindowSnapshot]:
    """Point the computer at the mirror app and return its window snapshot.
    `windows` lets a caller that already listed them (state) skip a second
    list_windows round-trip."""
    if windows is None:
        windows = await _mirror_windows(computer)
    if not windows:
        return None
    pid = next((int(w["pid"]) for w in windows if w.get("pid")), None)
    if pid is None:
        return None
    computer.target_pid = pid
    # the BASE geometry, never the PhoneComputer override — the override
    # re-enters ensure_ready, and this call sits inside ensure_ready's own
    # fresh preflight (override -> ensure_ready -> state -> here -> ∞)
    return await DriverComputer.window_geometry(computer, fresh_shot=fresh_shot)


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
            _hid_key(["return"])
        for ch in line:
            shifted = ch in _SHIFTED
            base = _SHIFTED.get(ch, ch)
            key = _KEY_NAMES.get(base, base)
            if len(key) == 1 and not (key.isalnum() and key.isascii()):
                raise ValueError(f"cannot type {ch!r} via keycodes")
            if shifted:
                _hid_key(["shift", key])
            else:
                _hid_key([key])
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
    await tap(computer, snap, hit.x, hit.y)
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
    x1, y1 = _screen_point(snap, cx - dx / 2, cy - dy / 2)
    x2, y2 = _screen_point(snap, cx + dx / 2, cy + dy / 2)
    _hid_drag(x1, y1, x2, y2, dur=0.12, steps=6)  # fast = a flick



# --- hands: raw global-HID events, NOT the driver ---
#
# VERIFIED 2026-08-09 on the iOS Simulator: tap_text navigated Settings
# (General -> About) via _hid_tap; chords post without error. Typing uses the
# identical CGEventPost(kCGHIDEventTap) channel (proven for taps here and for
# text in the phone-harness project). Live typing demo pending a real device /
# a reliably-focused field.
#
# MEASURED (2026-08-09, iOS Simulator + iPhone Mirroring): the driver's
# synthesized input reaches these video-stream windows at NO scope — not
# pid-addressed background, not foreground-assisted, not scope="desktop".
# Only raw CGEvents posted to the global HID tap (what phone-harness does)
# are picked up by WindowServer and forwarded to the phone as touches. So
# the driver is EYES here (capture / OCR / window discovery) and Quartz is
# HANDS. The window must be frontmost first (ensure_ready guarantees it).

def _post_mouse(etype, x, y):
    import Quartz
    ev = Quartz.CGEventCreateMouseEvent(
        None, etype, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _hid_tap(x, y):
    import Quartz, time as _t
    _post_mouse(Quartz.kCGEventMouseMoved, x, y); _t.sleep(0.05)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x, y); _t.sleep(0.06)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x, y)


def _hid_drag(x1, y1, x2, y2, steps=12, dur=0.25):
    import Quartz, time as _t
    _post_mouse(Quartz.kCGEventMouseMoved, x1, y1); _t.sleep(0.05)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x1, y1); _t.sleep(0.05)
    for i in range(1, steps + 1):
        f = i / steps
        _post_mouse(Quartz.kCGEventLeftMouseDragged,
                    x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
        _t.sleep(dur / steps)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x2, y2)


def _hid_key(key_names):
    """Press a chord by driver-key-name list, via raw HID keycodes.
    Modifiers (cmd/shift/option/ctrl) contribute flags, not keycodes; the
    non-modifier key is the one actually pressed."""
    import Quartz, time as _t
    mods = 0
    main_keys = []
    for k in key_names:
        kl = k.lower()
        if kl in _MODMASK:
            mods |= _MODMASK[kl]
        else:
            main_keys.append(kl)
    if not main_keys:
        raise ValueError(f"chord {key_names} has no non-modifier key")
    code = _KEYCODE.get(main_keys[-1])
    if code is None:
        raise ValueError(f"unmappable key {main_keys[-1]!r} in {key_names}")
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        # Flags describe the modifier state DURING the event: held on the
        # DOWN, released by the UP. Setting them on the up as well latches
        # the modifier on the iOS side — a chorded cmd+3 left cmd stuck and
        # every following letter became a silent shortcut instead of text
        # (measured live: typing vanished until the chord's up was clean).
        # Always set explicitly — even 0 — so nothing inherits stale state.
        Quartz.CGEventSetFlags(ev, mods if down else 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        _t.sleep(0.03)


def _keycodes():
    import Quartz
    codes = {"return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
             "left": 123, "right": 124, "down": 125, "up": 126,
             "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
             "8": 28, "9": 25, "0": 29,
             "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
             "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
             "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
             "j": 38, "k": 40, "n": 45, "m": 46,
             "period": 47, "comma": 43, "slash": 44, "semicolon": 41,
             "quote": 39, "leftbracket": 33, "rightbracket": 30,
             "backslash": 42, "minus": 27, "equal": 24, "grave": 50}
    mods = {"cmd": Quartz.kCGEventFlagMaskCommand,
            "shift": Quartz.kCGEventFlagMaskShift,
            "option": Quartz.kCGEventFlagMaskAlternate,
            "alt": Quartz.kCGEventFlagMaskAlternate,
            "ctrl": Quartz.kCGEventFlagMaskControl}
    return codes, mods


_KEYCODE, _MODMASK = _keycodes()


def _screen_point(snap: WindowSnapshot, px: float, py: float) -> tuple:
    """OCR gives screenshot-PIXEL coords; a global-HID (desktop-scope) click
    needs SCREEN points. Convert through the window's logical bounds."""
    sw, sh = snap.screenshot_size or (0, 0)
    if not sw or not sh:
        raise RuntimeError("mirror window has no screenshot dimensions")
    bx, by = snap.bounds.get("x", 0), snap.bounds.get("y", 0)
    bw, bh = snap.bounds.get("width") or sw, snap.bounds.get("height") or sh
    return bx + px * (bw / sw), by + py * (bh / sh)


async def tap(computer: DriverComputer, snap: WindowSnapshot,
              px: float, py: float) -> None:
    """Tap at screenshot-pixel (px, py) via a global-HID desktop click — the
    only channel the mirror window forwards to the phone."""
    sx, sy = _screen_point(snap, px, py)
    _hid_tap(sx, sy)


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
    _hid_key(["cmd", "1"])
    await asyncio.sleep(0.8)


async def app_switcher(computer: DriverComputer) -> None:
    await ensure_ready(computer)
    _hid_key(["cmd", "2"])
    await asyncio.sleep(0.8)


async def open_app(computer: DriverComputer, name: str) -> None:
    """Open an app via the mirror's Spotlight (Cmd+3)."""
    await ensure_ready(computer)
    _hid_key(["cmd", "3"])
    await asyncio.sleep(0.9)
    await type_text(computer, name)
    await asyncio.sleep(1.2)  # let results populate before committing
    _hid_key(["return"])


# --- PhoneComputer: the replay adapter ---
#
# A macro with a `"device": "phone"` header replays through this instead of a
# bare DriverComputer. It keeps the driver for EYES (window discovery, OCR
# capture via window_geometry) but overrides every HANDS method with raw
# global-HID delivery — the only channel the mirror window accepts. The
# replay engine, the verify gate, slots, and asserts all work unchanged: the
# existing click ladder's OCR rung calls click_window_pixel, which here
# converts the screenshot-pixel hit to a screen point and taps via CGEvent.

class PhoneComputer(DriverComputer):
    """DriverComputer with phone hands: eyes via the driver, input via raw HID.

    The AX-element click rung finds nothing (a mirror window has no AX tree),
    so clicks fall to the OCR anchor rung — which resolves through
    click_window_pixel below and lands a real touch."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._geom: Optional[WindowSnapshot] = None

    async def window_snapshot(self, with_screenshot: bool = False):
        # Target + front the mirror before any observation.
        snap = await ensure_ready(self)
        self.target_pid = snap.pid
        got = await super().window_snapshot(with_screenshot=with_screenshot)
        if got is not None:
            self._geom = got
        return got

    async def window_geometry(self, fresh_shot: bool = False):
        snap = await ensure_ready(self)
        self.target_pid = snap.pid
        got = await super().window_geometry(fresh_shot=fresh_shot)
        if got is not None:
            self._geom = got
        return got

    def _to_screen(self, px: float, py: float) -> tuple:
        if self._geom is None:
            raise RuntimeError("no phone geometry yet — snapshot first")
        return _screen_point(self._geom, px, py)

    async def click_window_pixel(self, pid, window_id, x, y):
        _hid_tap(*self._to_screen(x, y))
        return {"path": "hid", "effect": "unverifiable"}

    async def double_click_window_pixel(self, pid, window_id, x, y):
        sx, sy = self._to_screen(x, y)
        _hid_tap(sx, sy); await asyncio.sleep(0.12); _hid_tap(sx, sy)
        return {"path": "hid", "effect": "unverifiable"}

    right_click_window_pixel = click_window_pixel  # iOS has no right-click

    async def type_text(self, text: str) -> None:
        await type_text(self, text)

    async def key(self, keys: list) -> None:
        _hid_key(list(keys))

    async def scroll(self, direction: str, clicks: int = 3) -> None:
        await swipe(self, direction, distance=min(0.8, 0.2 * max(1, clicks)))

    async def open(self, target: str) -> None:
        await open_app(self, target)

    async def move(self, x: float, y: float) -> None:
        pass  # no overlay cursor on a phone

    async def drag(self, frm: dict, to: dict) -> dict:
        snap = self._geom or await self.window_geometry(fresh_shot=True)
        sw, sh = snap.screenshot_size or (0, 0)

        def pt(d):
            w = (d.get("win") or {})
            return _screen_point(snap, w.get("fx", 0) * sw, w.get("fy", 0) * sh)
        (x1, y1), (x2, y2) = pt(frm), pt(to)
        _hid_drag(x1, y1, x2, y2, dur=0.12, steps=6)
        return {"path": "hid", "effect": "unverifiable"}
