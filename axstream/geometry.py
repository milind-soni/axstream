"""Window-relative click geometry — the resize-aware pixel math.

A recorded pixel click is only replayable as long as the click's position
RELATIVE TO ITS WINDOW still means the same thing. Absolute global screen
coordinates survive nothing; window-local offsets survive a window move (the
executor already subtracts the live origin); this module makes them survive a
RESIZE too, and — critically — refuse honestly when they can't.

The recorded geometry rides in the op target:

  {"op":"act","do":"click","target":{"x":420,"y":312,
      "win":{"fx":0.27,"fy":0.31,"w":1396,"h":806}}}

`fx`/`fy` are the click as a fraction of the recorded window, `w`/`h` the
recorded window's logical size. A recorder that knows the window at capture
time emits `win` directly; for drafts that only carry absolute coords plus a
one-line header key `"window": {"x":..,"y":..,"w":..,"h":..}` (the recorded
window bounds), `annotate_window_relative` derives `win` at load time.

Remapping (`remap_offset`) is EDGE-ANCHORED, not proportional: real UIs pin
toolbars to the top, sidebars to the left, and Done buttons to the
bottom-right — a control near an edge keeps its distance to that edge when
the window grows, it does not drift proportionally. Each axis independently
anchors to whichever edge the recorded click was nearer. Content in the
middle of a resized window is genuinely ambiguous; that case belongs to the
OCR text-anchor rung, and beyond MAX_RESIZE_RATIO the remap refuses so a
wildly different window becomes an honest failure instead of a blind click.
"""

from __future__ import annotations

from typing import Optional

# beyond this size ratio (either axis, either direction) the recorded
# geometry no longer says anything trustworthy about the live window
MAX_RESIZE_RATIO = 1.6

# live size within this many logical points of the recorded size counts as
# "the same window" — replay uses the exact recorded offsets
SIZE_TOLERANCE = 2.0


class GeometryMismatch(ValueError):
    """The live window's geometry is too far from the recorded one to click."""


def window_fraction(gx: float, gy: float, bounds: dict) -> Optional[dict]:
    """Global logical point -> a `win` target fragment, given the recorded
    window bounds {x, y, w|width, h|height}. None if bounds are unusable or
    the point lies outside the window (a desktop/menu-bar click carries no
    window-relative meaning)."""
    w = bounds.get("w", bounds.get("width")) or 0
    h = bounds.get("h", bounds.get("height")) or 0
    if w <= 0 or h <= 0:
        return None
    fx = (gx - bounds.get("x", 0)) / w
    fy = (gy - bounds.get("y", 0)) / h
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    return {"fx": round(fx, 6), "fy": round(fy, 6), "w": w, "h": h}


def annotate_window_relative(actions: list[dict], window: dict) -> list[dict]:
    """Give every absolute-coordinate click/double_click a `win` fragment
    derived from the header's recorded window bounds. Ops that already carry
    `win` (recorder-emitted) keep theirs; non-click ops and ops without
    coordinates pass through untouched. Returns new op dicts — the caller's
    (file-loaded) list is never mutated."""
    if not isinstance(window, dict):
        return actions
    out = []
    for op in actions:
        target = op.get("target")
        if (op.get("do") in ("click", "double_click")
                and isinstance(target, dict)
                and "x" in target and "y" in target
                and not isinstance(target.get("win"), dict)):
            win = window_fraction(target["x"], target["y"], window)
            if win is not None:
                op = {**op, "target": {**target, "win": win}}
        out.append(op)
    return out


def remap_offset(win: dict, live_w: float, live_h: float) -> tuple[float, float, str]:
    """Recorded `win` fragment -> window-local logical offset in the LIVE
    window. Returns (dx, dy, mode) where mode is "exact" (live size matches
    the recording) or "anchored" (edge-anchored remap ran).

    Raises GeometryMismatch when the live window is too different to trust —
    the caller must surface that instead of clicking."""
    rw, rh = float(win.get("w") or 0), float(win.get("h") or 0)
    fx, fy = win.get("fx"), win.get("fy")
    if rw <= 0 or rh <= 0 or fx is None or fy is None:
        raise GeometryMismatch(f"unusable win fragment: {win!r}")
    if live_w <= 0 or live_h <= 0:
        raise GeometryMismatch(f"unusable live window size {live_w}x{live_h}")
    for live, rec, axis in ((live_w, rw, "width"), (live_h, rh, "height")):
        ratio = live / rec
        if ratio > MAX_RESIZE_RATIO or ratio < 1.0 / MAX_RESIZE_RATIO:
            raise GeometryMismatch(
                f"window {axis} changed {rec:.0f} -> {live:.0f} "
                f"({ratio:.2f}x) since recording — refusing to click blind")
    dx, dy = fx * rw, fy * rh
    if abs(live_w - rw) <= SIZE_TOLERANCE and abs(live_h - rh) <= SIZE_TOLERANCE:
        return dx, dy, "exact"
    if dx > rw / 2:
        dx = live_w - (rw - dx)  # nearer the right edge: keep right distance
    if dy > rh / 2:
        dy = live_h - (rh - dy)  # nearer the bottom edge: keep bottom distance
    if not (0 <= dx <= live_w and 0 <= dy <= live_h):
        raise GeometryMismatch(
            f"remapped point ({dx:.0f},{dy:.0f}) falls outside the live "
            f"{live_w:.0f}x{live_h:.0f} window — refusing to click blind")
    return dx, dy, "anchored"
