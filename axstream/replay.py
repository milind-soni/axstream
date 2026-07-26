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
  2. WINDOW-LOCAL PIXEL — no AX match: convert the recorded GLOBAL screen
     coords into the driver's window-local screenshot-pixel space and click
     pid-addressed (click(pid, window_id, x, y)).
  3. Neither resolves — the failure JSON, reason distinguishing "label not
     found in AX tree" from "no window".

Each progress line says which rung ran ("via": "ax_element" |
"window_pixel") and echoes the driver's own delivery evidence
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

from .ax import Snapshot, resolve_window_element
from .driver import window_pixels_from_screen
from .executor import Executor
from .macrofile import (
    MacroFile,
    MacroFileError,
    discover,
    load,
    macro_dirs,
    resolve_name,
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


async def _click_via_ladder(computer, op: dict) -> dict:
    """The click/double_click resolution ladder (HANDOVER §4b).

    1. AX ELEMENT: fresh window snapshot -> fuzzy label match ->
       click(pid, window_id, element_index). Re-snapshot before EVERY click:
       the driver's element_index cache is per-snapshot (click.rs).
    2. WINDOW-LOCAL PIXEL: recorded global screen coords converted into the
       driver's window-local screenshot-pixel space, clicked pid-addressed.
       NEVER scope="desktop" for macro replay.
    3. Neither resolves -> ReplayFailure ("label not found in AX tree" vs
       "no window").

    Returns the extra progress-line fields (via / resolved / driver echo)."""
    do = op["do"]
    target = op.get("target") or {}
    ax = target.get("ax") if isinstance(target.get("ax"), dict) else None
    has_label = bool(ax and (ax.get("title") or ax.get("role")))
    has_coords = "x" in target and "y" in target
    if not has_label and not has_coords:
        raise ReplayFailure(
            f"{do}: target has neither an ax label nor coordinates: {json.dumps(target)}")

    snap = await computer.window_snapshot()
    if snap is None:
        raise ReplayFailure(
            f"{do}: no window — the target app has no on-screen window "
            "(needed for both the AX-element path and the pixel fallback)")

    ax_error: Optional[str] = None
    if has_label:
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
                # action definitively did NOT run — safe to fall through to
                # the pixel rung when the op recorded coordinates
                ax_error = str(e)
                if not has_coords:
                    raise ReplayFailure(
                        f"{do}: AX element click failed ({ax_error}) — and the "
                        "op carries no recorded coordinates to fall back to")

    if has_coords:
        if snap.screenshot_size is None:
            # re-snapshot WITH a screenshot: its dimensions define the pixel
            # space of the driver's window-local click contract (and register
            # a downscale ratio consistent with those dimensions)
            snap = await computer.window_snapshot(with_screenshot=True)
        if snap is not None and snap.screenshot_size is None:
            # window captures fail transiently right after a click (observed
            # live on Notes); settle briefly and retry once
            await asyncio.sleep(0.3)
            snap = await computer.window_snapshot(with_screenshot=True)
        if snap is None or snap.screenshot_size is None:
            raise ReplayFailure(
                f"{do}: pixel fallback failed — could not size the window "
                "screenshot (get_window_state returned no dimensions)")
        wx, wy = window_pixels_from_screen(
            target["x"], target["y"], snap.bounds, snap.screenshot_size)
        if do == "click":
            res = await computer.click_window_pixel(snap.pid, snap.window_id, wx, wy)
        else:
            res = await computer.double_click_window_pixel(snap.pid, snap.window_id, wx, wy)
        line = {"via": "window_pixel"}
        if ax_error is not None:
            line["note"] = f"AX element click failed ({ax_error}); pid-addressed window-local pixel fallback"
        elif has_label:
            line["note"] = "label not found in AX tree; pid-addressed window-local pixel fallback"
        line.update(_driver_evidence(res))
        return line

    raise ReplayFailure(
        f"{do}: label not found in AX tree: {json.dumps(ax)} — and the op "
        "carries no recorded coordinates to fall back to")


async def run_actions(actions: list[dict], computer, emit: Emit = _print_json) -> int:
    """Execute a resolved (slot-filled) action list with structured progress.

    Returns the process exit code (0 ok, 1 failed). Emits one JSON object per
    action and a final summary — the failure summary carries failed_at/op/
    reason/completed so an agent can take over at the exact op."""
    executor = Executor(computer, Snapshot({}), allow_risky=True)
    completed = 0
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
                ax = (op.get("target") or {}).get("ax") or {}
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
                info = await _click_via_ladder(computer, op)
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
    emit({"ok": True, "completed": completed, "total": len(actions)})
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
        try:
            return await run_actions(actions, computer)
        finally:
            await computer.close()

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
