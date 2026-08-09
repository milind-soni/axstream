"""act — the click resolution ladder and per-op dispatch.

click/double_click/right_click targets resolve through a strict preference
ladder (never scope="desktop" — that steals the user's real mouse):

  1. AX ELEMENT — fresh get_window_state snapshot per click, fuzzy-match the
     target's ax label, then click(pid, window_id, element_index): an
     AXUIElementPerformAction — no cursor move, no focus steal, survives
     window moves, works on background windows.
  2. OCR TEXT ANCHOR — the label (target.text, else the ax title) is located
     in a fresh window screenshot via on-device Vision OCR (ocr.py) and the
     hit's center is clicked in screenshot-pixel space. Finds "New Note"
     when the truncated AX walk can't; the click is target-VERIFIED — the
     text was just seen there. Soft rung: skipped without pyobjc installed.
  3. VISUAL PATCH ANCHOR — a target.patch fragment (a small grayscale crop of
     the control, learned via `replay --learn`) is re-located near its
     recorded spot in the fresh window screenshot by template match
     (patch.py: raw grayscale, then Canny edges for theme robustness).
     Verifies ICON-ONLY targets that OCR cannot see; same verified grade as
     an OCR hit. Soft rung: skipped without opencv installed.
  4. WINDOW-RELATIVE PIXEL — a target.win fragment (or one derived from the
     header's recorded "window" bounds) remaps the recorded click into the
     live window edge-anchored (geometry.py), surviving both window moves
     and resizes; a wildly different window REFUSES instead of clicking
     blind. Plain global x/y without recorded window bounds translate
     against the live origin as before (move-safe, resize-blind).
     Pixel clicks skip the element walk entirely — window geometry comes
     from a max_elements=1 snapshot with a size-keyed dims cache
     (driver.window_geometry): ~15ms per repeat click vs ~3s before.

Each rung's progress-line fields say which rung ran ("via": "ax_element" |
"ocr_anchor" | "patch_anchor" | "window_pixel") and echo the driver's own
delivery evidence ("driver_path": "ax" | "cgevent" ..., "effect"). `move`
resolves ax-label-then-coords against the observation snapshot (it only
drives the driver's overlay cursor).
"""

from __future__ import annotations

import asyncio
import json

from . import ocr
from . import patch as patchmod
from .ax import resolve_window_element
from .driver import window_pixels_from_screen
from .geometry import GeometryMismatch, remap_offset


class ReplayFailure(RuntimeError):
    """An action-level failure with an agent-readable reason."""


async def resolve_move(computer, op: dict):
    """Resolve a `move` target to concrete coords (the driver's overlay
    cursor is visual-only, so screen coordinates are fine here). AX label
    first against a fresh window snapshot, recorded coordinates second."""
    target = op.get("target") or {}
    ax = target.get("ax") if isinstance(target.get("ax"), dict) else None
    if ax and (ax.get("role") or ax.get("title")):
        snap = await computer.window_snapshot()
        el = resolve_window_element(ax, snap.elements) if snap else None
        if el is not None and el.get("frame"):
            f = el["frame"]
            return (f["x"] + f.get("w", 0) / 2, f["y"] + f.get("h", 0) / 2,
                    "ax", f"{el.get('role','')} {el.get('label','')!r}")
    if "x" in target and "y" in target:
        via = "coords_fallback" if ax else "coords"
        return target["x"], target["y"], via, None
    raise ReplayFailure(f"move: could not resolve target {json.dumps(target)}")


def driver_evidence(res: object) -> dict:
    """Compact echo of WHICH driver path delivered the action, straight off
    the tool result: click.rs sets structuredContent {"path": "ax" | "ax_fg"
    | "cgevent" | "cgevent_fg" | "cgevent_hid", "effect": "unverifiable" |
    "suspected_noop"}; double_click's AX rung reports prose only."""
    out: dict = {}
    if not isinstance(res, dict):
        return out
    if res.get("path"):
        out["driver_path"] = res["path"]
    if res.get("effect"):
        out["effect"] = res["effect"]
    if not out and isinstance(res.get("text"), str) and res["text"]:
        out["driver_text"] = res["text"][:160]
    return out


async def _click_dispatch(computer, do: str, pid: int, window_id: int,
                          x: float, y: float) -> dict:
    if do == "click":
        return await computer.click_window_pixel(pid, window_id, x, y)
    if do == "double_click":
        return await computer.double_click_window_pixel(pid, window_id, x, y)
    return await computer.right_click_window_pixel(pid, window_id, x, y)


async def click_via_ladder(computer, op: dict, learn: bool = False) -> dict:
    """The click/double_click resolution ladder (see the module docstring):
    AX element -> OCR text anchor -> patch anchor -> window-relative pixel.
    Returns the extra progress-line fields (via / resolved / notes / driver
    echo). With learn=True, ocr_anchor and window_pixel clicks also capture
    a visual patch anchor from the pre-click screenshot and return it under
    "_learned_patch" for the caller to persist (patch.py; capture refuses
    ambiguous controls, so a missing fragment is honest, not an error)."""
    do = op["do"]
    target = op.get("target") or {}
    ax = target.get("ax") if isinstance(target.get("ax"), dict) else None
    has_label = bool(ax and (ax.get("title") or ax.get("role")))
    anchor = ""
    if isinstance(target.get("text"), str):
        anchor = target["text"].strip()
    if not anchor and ax and isinstance(ax.get("title"), str):
        anchor = ax["title"].strip()
    win = target.get("win") if isinstance(target.get("win"), dict) else None
    patch_frag = target.get("patch") if isinstance(target.get("patch"), dict) else None
    has_coords = ("x" in target and "y" in target) or win is not None
    if not has_label and not anchor and not has_coords:
        raise ReplayFailure(
            f"{do}: target has no ax label, no text anchor, and no "
            f"coordinates: {json.dumps(target)}")

    notes: list[str] = []

    # ── Rung 1: AX element (the only rung that needs the element walk) ──
    if has_label:
        snap = await computer.window_snapshot(with_screenshot=False)
        if snap is None:
            raise ReplayFailure(
                f"{do}: no window — the target app has no window to act on")
        el = resolve_window_element(ax, snap.elements)
        if el is not None:
            idx = el["element_index"]
            try:
                if do == "click":
                    res = await computer.click_element(snap.pid, snap.window_id, idx)
                elif do == "double_click":
                    res = await computer.double_click_element(snap.pid, snap.window_id, idx)
                else:
                    res = await computer.right_click_element(snap.pid, snap.window_id, idx)
                line = {"via": "ax_element",
                        "resolved": f"{el.get('role', '')} {el.get('label', '')!r} [element {idx}]"}
                line.update(driver_evidence(res))
                return line
            except Exception as e:  # noqa: BLE001 - a hard AX error (e.g.
                # kAXErrorActionUnsupported on Notes list cells) means the
                # action definitively did NOT run — safe to continue down
                # the ladder
                notes.append(f"AX element click failed ({e})")
        else:
            notes.append("label not found in AX tree")

    # ── Rung 2: OCR text anchor — needs the window's CURRENT pixels ──
    geom = None
    if anchor:
        if ocr.available():
            geom = await computer.window_geometry(fresh_shot=True)
            if geom is not None and geom.fresh_shot and geom.shot_path:
                hit = ocr.find_text(geom.shot_path, anchor)
                if hit is not None:
                    learned = None
                    if learn and patch_frag is None and patchmod.available():
                        # pre-click pixels are what a future replay will see
                        learned = patchmod.capture_patch(
                            geom.shot_path, hit.x, hit.y)
                    res = await _click_dispatch(
                        computer, do, geom.pid, geom.window_id, hit.x, hit.y)
                    line = {"via": "ocr_anchor",
                            "resolved": (f"text {hit.text!r} @({hit.x:.0f},"
                                         f"{hit.y:.0f})px [{hit.level} ocr, "
                                         f"conf {hit.confidence:.2f}]")}
                    if learned is not None:
                        line["_learned_patch"] = learned
                    if notes:
                        line["note"] = "; ".join(notes)
                    line.update(driver_evidence(res))
                    return line
                notes.append(f"OCR did not find {anchor!r} on screen")
            else:
                notes.append("OCR skipped: could not capture the window")
        else:
            notes.append("OCR unavailable (pip install axstream)")

    # ── Rung 2.5: visual patch anchor — verifies what OCR cannot see ──
    # (icon-only controls have no rendered text; a learned template crop
    # re-located near its recorded spot is the same grade of verification
    # as an OCR hit: the control was just seen there)
    if patch_frag is not None:
        if patchmod.available():
            if geom is None or not (geom.fresh_shot and geom.shot_path):
                geom = await computer.window_geometry(fresh_shot=True)
            if geom is not None and geom.fresh_shot and geom.shot_path:
                phit = patchmod.find_patch(geom.shot_path, patch_frag)
                if phit is not None:
                    res = await _click_dispatch(
                        computer, do, geom.pid, geom.window_id, phit.x, phit.y)
                    line = {"via": "patch_anchor",
                            "resolved": (f"patch @({phit.x:.0f},{phit.y:.0f})px "
                                         f"[{phit.method} match {phit.score:.2f}]")}
                    if notes:
                        line["note"] = "; ".join(notes)
                    line.update(driver_evidence(res))
                    return line
                notes.append("patch anchor did not match near its recorded spot")
            else:
                notes.append("patch skipped: could not capture the window")
        else:
            notes.append("patch unavailable (pip install 'axstream[patch]')")

    # ── Rung 3: window-relative / window-local pixels ──
    if has_coords:
        if geom is None or geom.screenshot_size is None:
            geom = await computer.window_geometry()
        if geom is None:
            raise ReplayFailure(
                f"{do}: no window — the target app has no window to act on")
        if geom.screenshot_size is None:
            # window captures fail transiently right after a click or key
            # press (observed live on Notes, sometimes for >0.3s); settle
            # with backoff before giving up
            for delay in (0.3, 0.6):
                await asyncio.sleep(delay)
                geom = await computer.window_geometry()
                if geom is not None and geom.screenshot_size is not None:
                    break
        if geom is None or geom.screenshot_size is None:
            raise ReplayFailure(
                f"{do}: pixel fallback failed — could not size the window "
                "screenshot (get_window_state returned no dimensions)")
        if (learn and patch_frag is None and patchmod.available()
                and not (geom.fresh_shot and geom.shot_path)):
            # learning needs the window's current pixels; only the learn run
            # pays this extra capture
            fresh = await computer.window_geometry(fresh_shot=True)
            if fresh is not None and fresh.screenshot_size is not None:
                geom = fresh
        sw, sh = geom.screenshot_size
        lw = geom.bounds.get("width") or 0
        lh = geom.bounds.get("height") or 0
        line = {"via": "window_pixel"}
        if win is not None:
            try:
                dx, dy, mode = remap_offset(win, lw, lh)
            except GeometryMismatch as e:
                # the recorded absolute coords come from the same recording,
                # so they are just as stale — refuse rather than click blind
                raise ReplayFailure(f"{do}: {e}") from e
            wx = dx * (sw / lw) if lw else dx
            wy = dy * (sh / lh) if lh else dy
            line["geometry"] = mode
            if mode == "anchored" and ocr.available():
                # bubble-cursor snap: an edge-anchored remap is APPROXIMATE
                # (the window was resized since recording), so pull the click
                # onto the nearest unambiguous text line within ~a control's
                # height — a near-miss becomes a hit on the visible target,
                # and an ambiguous neighborhood is left untouched
                fresh = geom if geom.fresh_shot else \
                    await computer.window_geometry(fresh_shot=True)
                if (fresh is not None and fresh.fresh_shot
                        and fresh.shot_path and fresh.screenshot_size):
                    snap = ocr.nearest_text(fresh.shot_path, wx, wy,
                                            max_dist=0.03 * fresh.screenshot_size[0])
                    if snap is not None:
                        geom, (wx, wy) = fresh, (snap.x, snap.y)
                        line["snapped_to"] = snap.text
        else:
            wx, wy = window_pixels_from_screen(
                target["x"], target["y"], geom.bounds, geom.screenshot_size)
        learned = None
        if learn and patch_frag is None and geom.fresh_shot and geom.shot_path:
            # a blind click can still RECORD what it aimed at — the learned
            # patch makes the NEXT run verified (or an honest miss if the
            # spot never looked like this again)
            learned = patchmod.capture_patch(geom.shot_path, wx, wy)
        res = await _click_dispatch(computer, do, geom.pid, geom.window_id, wx, wy)
        if learned is not None:
            line["_learned_patch"] = learned
        if notes:
            line["note"] = "; ".join(notes)
        line.update(driver_evidence(res))
        return line

    raise ReplayFailure(
        f"{do}: could not resolve the target ({'; '.join(notes)}) — and the "
        "op carries no recorded coordinates to fall back to")
