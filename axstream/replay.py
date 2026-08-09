"""`axstream replay` / `axstream list` — the agent-facing file-macro CLI.

Replay executes a macro file (or a raw draft) through DriverComputer — the
proven cua-driver edge (see HANDOVER §4b; computer-server is flaky) — and
speaks JSONL back: one progress object per action, and on failure a final

  {"failed_at": <index>, "op": {...}, "reason": "...", "completed": <n>}

with a non-zero exit, so a coding agent knows exactly which action to take
over from. Click targets resolve through the verified ladder in act.py;
asserts and wait_until poll through check.py.

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

from . import patch as patchmod
from .act import ReplayFailure, click_via_ladder, driver_evidence, resolve_move
from .check import ConditionTimeout, wait_for_target
from .geometry import annotate_window_relative
from .macrofile import (
    computer_for,
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


async def run_actions(actions: list[dict], computer, emit: Emit = _print_json,
                      learn: bool = False,
                      learned: Optional[dict[int, dict]] = None) -> int:
    """Execute a resolved (slot-filled) action list with structured progress.

    Returns the process exit code (0 ok, 1 failed). Emits one JSON object per
    action and a final summary — the failure summary carries failed_at/op/
    reason/completed so an agent can take over at the exact op.

    learn=True captures a visual patch anchor per verified-position click
    (see act.click_via_ladder); fragments land in the caller-supplied
    `learned` dict keyed by action index, ready for macrofile.save_patches."""
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
                try:
                    info = await wait_for_target(
                        computer, target,
                        timeout_ms=op.get("timeout_ms", 2500),
                        poll_ms=op.get("poll_ms", 120),
                    )
                except ConditionTimeout as exc:
                    raise ReplayFailure(f"assert failed: {exc}") from exc
                emit({"i": i, "op": op, "ok": True,
                      "ms": int((time.perf_counter() - t0) * 1000), **info})
                completed += 1
                continue
            # kind == "act"
            if op.get("do") == "wait_until":
                try:
                    info = await wait_for_target(
                        computer, op.get("target") or {},
                        timeout_ms=op.get("timeout_ms", 2500),
                        poll_ms=op.get("poll_ms", 120),
                    )
                except ConditionTimeout as exc:
                    raise ReplayFailure(f"wait_until failed: {exc}") from exc
                emit({"i": i, "op": op, "ok": True,
                      "ms": int((time.perf_counter() - t0) * 1000), **info})
                completed += 1
                continue
            if op.get("do") in ("click", "double_click", "right_click"):
                info = await click_via_ladder(computer, op, learn=learn)
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
            if op.get("do") == "drag":
                info = await computer.drag(op["from"], op["to"])
                blind.append(i)
                line = {"i": i, "op": op, "ok": True,
                        "ms": int((time.perf_counter() - t0) * 1000)}
                if isinstance(info, dict):
                    for field in ("via", "geometry"):
                        if field in info:
                            line[field] = info[field]
                    line.update(driver_evidence(info))
                emit(line)
                completed += 1
                continue
            do = op.get("do")
            line: dict = {"i": i, "op": op, "ok": True}
            if do == "type":
                await computer.type_text(op["text"])
            elif do == "key":
                await computer.key(op["keys"])
            elif do == "scroll":
                await computer.scroll(op["direction"], op.get("clicks", 3))
            elif do == "open":
                await computer.open(op["target"])
            elif do == "wait":
                await asyncio.sleep(op.get("ms", 300) / 1000)
            elif do == "move":
                x, y, via, resolved = await resolve_move(computer, op)
                await computer.move(x, y)
                line["via"] = via
                if resolved:
                    line["resolved"] = resolved
            else:
                raise ReplayFailure(f"unknown action {do!r}")
            line["ms"] = int((time.perf_counter() - t0) * 1000)
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
        summary["note"] = (f"{len(blind)} step(s) acted without target "
                           "verification (blind pixel gesture or suspected "
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

    if args.learn and not patchmod.available():
        # --learn would otherwise no-op SILENTLY (measured live: a full run,
        # zero anchors, no hint why) — say so up front, then replay normally
        _print_json({"warning": "--learn needs opencv (pip install "
                     "'axstream[patch]') — replaying without learning"})

    async def go() -> int:
        computer = computer_for(mf)
        if mf.extra.get("delivery") == "foreground": computer.delivery = "foreground"
        await computer.connect()
        await computer.fast_cursor()
        learned: dict[int, dict] = {}
        try:
            return await run_actions(actions, computer,
                                     learn=args.learn, learned=learned,
                                     emit=lambda line: (events.append(line),
                                                        _print_json(line))[1])
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

    events: list[dict] = []
    started = time.perf_counter()
    code = asyncio.run(go())
    from .ledger import record
    record(mf.name, code, round((time.perf_counter() - started) * 1000), events)
    return code


def cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="axstream list", description="List macro files found in "
        "./.axstream/macros and ~/.axstream/macros.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="one JSON object per macro")
    args = parser.parse_args(argv)

    from .gate import verification

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
                         "slot_specs": mf.slots,
                         "verified": verification(mf)["state"],
                         "provenance": mf.provenance, "actions": len(mf.actions),
                         "file": str(path)})
        else:
            slot_part = f" ({{{slots}}})" if slots else ""
            desc = mf.description or mf.when_to_use
            state = verification(mf)["state"]
            trust = {"verified": "✓", "unverified": "○", "stale": "↻"}.get(state, "!")
            print(f"{trust} {mf.name:22}{slot_part:20} {desc}  [{path}]")
    return 0
