"""`axstream replay` / `axstream list` — the agent-facing file-macro CLI.

Replay executes a macro file (or a raw draft) through DriverComputer — the
proven cua-driver edge (see HANDOVER §4b; computer-server is flaky) — and
speaks JSONL back: one progress object per action, and on failure a final

  {"failed_at": <index>, "op": {...}, "reason": "...", "completed": <n>}

with a non-zero exit, so a coding agent knows exactly which action to take
over from.

click/double_click targets resolve through a strict preference ladder
(never scope="desktop" — that steals the user's real mouse):

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

Each progress line says which rung ran ("via": "ax_element" | "ocr_anchor" |
"patch_anchor" | "window_pixel") and echoes the driver's own delivery evidence
("driver_path": "ax" | "cgevent" ..., "effect"). `move` still resolves
ax-label-then-coords against the observation snapshot (it only drives the
driver's overlay cursor).

Exit codes: 0 ok · 1 an action failed (failure JSON printed) · 2 usage /
file / slot errors (an {"error": ...} JSON line printed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Callable, Optional

from . import ocr
from . import patch as patchmod
from .ax import Snapshot, resolve_window_element
from .driver import window_pixels_from_screen
from .executor import Executor
from .geometry import GeometryMismatch, annotate_window_relative, remap_offset
from .macrofile import (
    MacroFile,
    MacroFileError,
    discover,
    load,
    macro_dirs,
    resolve_name,
    save_patches,
)

Emit = Callable[[dict], None]


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


class ReplayFailure(RuntimeError):
    """An action-level failure with an agent-readable reason."""


async def _resolve_act(executor: Executor, op: dict) -> tuple[dict, Optional[str], Optional[str]]:
    """Resolve a `move` target to concrete coords (the driver's overlay
    cursor is visual-only, so screen coordinates are fine here).
    click/double_click do NOT pass through here — see _click_via_ladder.

    Order: AX label (fuzzy, live tree, one refresh) -> recorded coordinates.
    Returns (executable op, via, resolved-element description)."""
    do = op.get("do")
    target = op.get("target")
    if do != "move" or not isinstance(target, dict):
        return op, None, None
    ax = target.get("ax")
    if isinstance(ax, dict) and (ax.get("role") or ax.get("title") or ax.get("id")):
        el = executor.snapshot.resolve_element(ax) or await executor._refresh_and_resolve(ax)
        if el is not None and el.center is not None:
            resolved = {**op, "target": {"x": el.center[0], "y": el.center[1]}}
            return resolved, "ax", f"{el.role} {el.title!r}"
    if "x" in target and "y" in target:
        via = "coords_fallback" if isinstance(ax, dict) else "coords"
        return {**op, "target": {"x": target["x"], "y": target["y"]}}, via, None
    raise ReplayFailure(f"could not resolve target {json.dumps(target)}")


def _driver_evidence(res: object) -> dict:
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
    return await computer.double_click_window_pixel(pid, window_id, x, y)


async def _click_via_ladder(computer, op: dict, learn: bool = False) -> dict:
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
                else:
                    res = await computer.double_click_element(snap.pid, snap.window_id, idx)
                line = {"via": "ax_element",
                        "resolved": f"{el.get('role', '')} {el.get('label', '')!r} [element {idx}]"}
                line.update(_driver_evidence(res))
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
                    line.update(_driver_evidence(res))
                    return line
                notes.append(f"OCR did not find {anchor!r} on screen")
            else:
                notes.append("OCR skipped: could not capture the window")
        else:
            notes.append("OCR unavailable (pip install 'axstream[ocr]')")

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
                    line.update(_driver_evidence(res))
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
        line.update(_driver_evidence(res))
        return line

    raise ReplayFailure(
        f"{do}: could not resolve the target ({'; '.join(notes)}) — and the "
        "op carries no recorded coordinates to fall back to")


async def run_actions(actions: list[dict], computer, emit: Emit = _print_json,
                      learn: bool = False,
                      learned: Optional[dict[int, dict]] = None) -> int:
    """Execute a resolved (slot-filled) action list with structured progress.

    Returns the process exit code (0 ok, 1 failed). Emits one JSON object per
    action and a final summary — the failure summary carries failed_at/op/
    reason/completed so an agent can take over at the exact op.

    learn=True captures a visual patch anchor per verified-position click
    (see _click_via_ladder); fragments land in the caller-supplied `learned`
    dict keyed by action index, ready for macrofile.save_patches."""
    executor = Executor(computer, Snapshot({}), allow_risky=True)
    completed = 0
    # A delivered action is not a LANDED action: the driver reports
    # effect:"unverifiable" when it cannot read back the result (a key press,
    # or a pixel click it cannot confirm hit anything). Counting those keeps
    # the summary honest instead of reporting a blind run as a clean success.
    blind: list[int] = []  # indices of steps whose TARGET was never verified
    for i, op in enumerate(actions):
        kind = op.get("op")
        t0 = time.perf_counter()
        try:
            if kind == "done":
                emit({"i": i, "op": op, "ok": True})
                completed += 1
                break
            if kind == "observe":
                emit({"i": i, "op": op, "ok": True,
                      "note": "observe is a no-op in file replay"})
                completed += 1
                continue
            if kind == "assert":
                target = op.get("target") or {}
                if isinstance(target.get("text"), str) and target["text"].strip():
                    # OCR outcome assertion: the text must be VISIBLE in the
                    # window (e.g. assert the results page shows the typed
                    # query). Polls briefly — pages/UI need a beat to render.
                    hit = None
                    for delay in (0.0, 0.4, 0.8, 1.2):
                        if delay:
                            await asyncio.sleep(delay)
                        g = await computer.window_geometry(fresh_shot=True)
                        if g is not None and g.fresh_shot and g.shot_path:
                            hit = ocr.find_text(g.shot_path, target["text"])
                            if hit is not None:
                                break
                    if hit is None:
                        suffix = ("" if ocr.available() else
                                  " (OCR unavailable — pip install 'axstream[ocr]')")
                        raise ReplayFailure(
                            f"assert failed: text {target['text']!r} not "
                            f"visible in the window{suffix}")
                    emit({"i": i, "op": op, "ok": True, "via": "ocr",
                          "resolved": f"text {hit.text!r} visible [{hit.level} ocr]",
                          "ms": int((time.perf_counter() - t0) * 1000)})
                    completed += 1
                    continue
                ax = target.get("ax") or {}
                el = executor.snapshot.resolve_element(ax) or await executor._refresh_and_resolve(ax)
                if el is None:
                    raise ReplayFailure(
                        f"assert failed: target did not resolve: {json.dumps(op.get('target'))}")
                emit({"i": i, "op": op, "ok": True, "via": "ax",
                      "resolved": f"{el.role} {el.title!r}",
                      "ms": int((time.perf_counter() - t0) * 1000)})
                completed += 1
                continue
            # kind == "act"
            if op.get("do") in ("click", "double_click"):
                info = await _click_via_ladder(computer, op, learn=learn)
                fragment = info.pop("_learned_patch", None)
                if fragment is not None and learned is not None:
                    learned[i] = fragment
                    info["learned_patch"] = True
                # ax_element, ocr_anchor and patch_anchor clicks are
                # target-VERIFIED (the element / rendered text / control
                # pixels were just resolved in the live UI — the driver
                # merely can't confirm the click's downstream effect);
                # window_pixel clicks verified nothing, and a
                # suspected_noop AX press likely did nothing at all
                if (info.get("via") == "window_pixel"
                        or info.get("effect") == "suspected_noop"):
                    blind.append(i)
                emit({"i": i, "op": op, "ok": True,
                      "ms": int((time.perf_counter() - t0) * 1000), **info})
                completed += 1
                continue
            exec_op, via, resolved = await _resolve_act(executor, op)
            await executor._execute(exec_op)
            line: dict = {"i": i, "op": op, "ok": True,
                          "ms": int((time.perf_counter() - t0) * 1000)}
            if via:
                line["via"] = via
            if resolved:
                line["resolved"] = resolved
            emit(line)
            completed += 1
        except Exception as e:  # noqa: BLE001 - every failure becomes the handoff JSON
            reason = str(e) if isinstance(e, ReplayFailure) else f"{op.get('do', kind)}: {e}"
            emit({"i": i, "op": op, "ok": False, "reason": reason})
            emit({"failed_at": i, "op": op, "reason": reason, "completed": completed})
            return 1
    summary = {"ok": True, "completed": completed, "total": len(actions)}
    if blind:
        # surfaced so a caller (or agent) knows to confirm the end state
        summary["unverified_steps"] = blind
        summary["note"] = (f"{len(blind)} step(s) clicked without target "
                           "verification (blind pixel click or suspected "
                           "no-op) — confirm the end state before trusting "
                           "this run")
    emit(summary)
    return 0


# -- CLI ------------------------------------------------------------------


def cmd_replay(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="axstream replay",
        description="Replay a macro file (or raw draft) through cua-driver.")
    parser.add_argument("target", help="a macro name (searched in "
                        "./.axstream/macros then ~/.axstream/macros) or a file path")
    parser.add_argument("--slots", default=None,
                        help='slot values as JSON, e.g. \'{"title":"standup"}\'')
    parser.add_argument("--dry", action="store_true",
                        help="print the resolved action list without executing")
    parser.add_argument("--learn", action="store_true",
                        help="capture visual patch anchors for clicks that "
                        "lack one and save them back into the macro file — "
                        "run this once while confirming the macro works; "
                        "later replays verify icon-only targets by patch")
    args = parser.parse_args(argv)

    slots: dict = {}
    if args.slots:
        try:
            slots = json.loads(args.slots)
            if not isinstance(slots, dict):
                raise ValueError("not an object")
        except ValueError as e:
            _print_json({"error": f"--slots must be a JSON object: {e}"})
            return 2

    path = resolve_name(args.target)
    if path is None:
        _print_json({"error": f"no macro named {args.target!r}",
                     "searched": [str(d) for d in macro_dirs()]})
        return 2
    try:
        mf = load(path)
        example_slots: list[str] = []
        if args.dry:
            # Nothing executes in a dry run, so header example values stand in
            # for any slot the caller didn't pass — a slotted macro must be
            # verifiable without real inputs.
            for slot_name, spec in (mf.slots or {}).items():
                if slot_name in slots:
                    continue
                example = spec.get("example") if isinstance(spec, dict) else None
                slots[slot_name] = (
                    str(example) if example not in (None, "") else f"<{slot_name}>"
                )
                example_slots.append(slot_name)
        actions = mf.fill(slots)
        # a recorded "window" header key (the window bounds at capture time)
        # makes every absolute-coordinate click resize-aware — see geometry.py
        recorded_window = mf.extra.get("window")
        if isinstance(recorded_window, dict):
            actions = annotate_window_relative(actions, recorded_window)
    except MacroFileError as e:
        _print_json({"error": str(e), "file": str(path)})
        return 2

    if args.dry:
        for i, op in enumerate(actions):
            _print_json({"i": i, "op": op, "dry": True})
        summary = {"dry": True, "ok": True, "macro": mf.name,
                   "file": str(path), "actions": len(actions)}
        if example_slots:
            summary["example_slots"] = example_slots
        _print_json(summary)
        return 0

    from .driver import DriverComputer  # imported late: not needed for --dry

    async def go() -> int:
        computer = DriverComputer()
        await computer.connect()
        await computer.fast_cursor()
        learned: dict[int, dict] = {}
        try:
            return await run_actions(actions, computer,
                                     learn=args.learn, learned=learned)
        finally:
            await computer.close()
            if learned:
                # persist even after a mid-run failure — anchors for the
                # steps that DID click are just as valid
                try:
                    n = save_patches(path, learned)
                    _print_json({"learned_patches": n, "file": str(path)})
                except (OSError, ValueError) as e:
                    _print_json({"learned_patches": 0,
                                 "error": f"could not save patches: {e}"})

    return asyncio.run(go())


def cmd_bench(argv: list[str]) -> int:
    """`axstream bench <macro>` — replay a macro N times and report per-op
    and total latency (p50/p95/min/max). Warmup runs execute but don't
    count, absorbing cold app launches so steady-state numbers are honest."""
    parser = argparse.ArgumentParser(
        prog="axstream bench",
        description="Benchmark a macro: replay repeatedly, report per-op latency.")
    parser.add_argument("target", help="a macro name or file path")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--slots", default=None,
                        help="slot values as JSON (same values every run)")
    args = parser.parse_args(argv)

    slots: dict = {}
    if args.slots:
        try:
            slots = json.loads(args.slots)
        except ValueError as e:
            _print_json({"error": f"--slots must be a JSON object: {e}"})
            return 2
    path = resolve_name(args.target)
    if path is None:
        _print_json({"error": f"no macro named {args.target!r}"})
        return 2
    try:
        mf = load(path)
        for slot_name, spec in (mf.slots or {}).items():
            slots.setdefault(slot_name, str(
                (spec.get("example") if isinstance(spec, dict) else None)
                or f"<{slot_name}>"))
        actions = mf.fill(slots)
        recorded_window = mf.extra.get("window")
        if isinstance(recorded_window, dict):
            actions = annotate_window_relative(actions, recorded_window)
    except MacroFileError as e:
        _print_json({"error": str(e), "file": str(path)})
        return 2

    from .driver import DriverComputer  # late: not needed for usage errors

    def pct(vals: list[float], p: float) -> int:
        s = sorted(vals)
        return int(s[min(len(s) - 1, int(round(p * (len(s) - 1))))])

    async def go() -> int:
        computer = DriverComputer()
        await computer.connect()
        await computer.fast_cursor()
        per_op: dict[int, list[float]] = {}
        totals: list[float] = []
        failures = 0
        try:
            for run in range(args.warmup + args.runs):
                warm = run < args.warmup
                events: list[dict] = []
                t0 = time.perf_counter()
                code = await run_actions(actions, computer, emit=events.append)
                total = (time.perf_counter() - t0) * 1000
                label = "warmup" if warm else "run"
                _print_json({label: run + 1 if warm else run + 1 - args.warmup,
                             "ok": code == 0, "total_ms": int(total)})
                if warm:
                    continue
                if code != 0:
                    failures += 1
                    continue
                totals.append(total)
                for ev in events:
                    if "i" in ev and "ms" in ev:
                        per_op.setdefault(ev["i"], []).append(ev["ms"])
        finally:
            await computer.close()
        if not totals:
            _print_json({"error": "no successful measured runs", "failures": failures})
            return 1
        for i, op in enumerate(actions):
            vals = per_op.get(i)
            if not vals:
                continue
            _print_json({"op": i, "do": op.get("do", op.get("op")),
                         "p50_ms": pct(vals, 0.5), "p95_ms": pct(vals, 0.95),
                         "min_ms": int(min(vals)), "max_ms": int(max(vals)),
                         "n": len(vals)})
        _print_json({"macro": mf.name, "runs": len(totals), "failures": failures,
                     "total_p50_ms": pct(totals, 0.5),
                     "total_p95_ms": pct(totals, 0.95),
                     "total_min_ms": int(min(totals)),
                     "total_max_ms": int(max(totals))})
        return 0

    return asyncio.run(go())


def cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="axstream list", description="List macro files found in "
        "./.axstream/macros and ~/.axstream/macros.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="one JSON object per macro")
    args = parser.parse_args(argv)

    found = discover()
    if not found:
        dirs = ", ".join(str(d) for d in macro_dirs())
        print(f"no macros found (searched {dirs})", file=sys.stderr)
        return 0
    for path, mf in found:
        if isinstance(mf, MacroFileError):
            if args.as_json:
                _print_json({"file": str(path), "error": str(mf)})
            else:
                print(f"{path.stem:24} [broken: {mf}]  {path}")
            continue
        slots = ",".join(sorted(mf.used_slots() & set(mf.slots)) or sorted(mf.slots))
        if args.as_json:
            _print_json({"name": mf.name, "description": mf.description,
                         "when_to_use": mf.when_to_use, "slots": sorted(mf.slots),
                         "provenance": mf.provenance, "actions": len(mf.actions),
                         "file": str(path)})
        else:
            slot_part = f" ({{{slots}}})" if slots else ""
            desc = mf.description or mf.when_to_use
            print(f"{mf.name:24}{slot_part:20} {desc}  [{path}]")
    return 0
