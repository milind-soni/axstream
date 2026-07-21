"""`axstream replay` / `axstream list` — the agent-facing file-macro CLI.

Replay executes a macro file (or a raw draft) through DriverComputer — the
proven cua-driver edge (see HANDOVER §4b; computer-server is flaky) — and
speaks JSONL back: one progress object per action, and on failure a final

  {"failed_at": <index>, "op": {...}, "reason": "...", "completed": <n>}

with a non-zero exit, so a coding agent knows exactly which action to take
over from. Target resolution for click/double_click/move tries the AX label
first (fuzzy, against the live tree) and falls back to recorded coordinates;
each progress line says which path was used ("via": "ax" | "coords" |
"coords_fallback").

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

from .ax import Snapshot
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
    """Resolve a coordinate-taking act to concrete coords.

    Order: AX label (fuzzy, live tree, one refresh) -> recorded coordinates.
    Returns (executable op, via, resolved-element description)."""
    do = op.get("do")
    target = op.get("target")
    if do not in ("click", "double_click", "move") or not isinstance(target, dict):
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
        actions = mf.fill(slots)
    except MacroFileError as e:
        _print_json({"error": str(e), "file": str(path)})
        return 2

    if args.dry:
        for i, op in enumerate(actions):
            _print_json({"i": i, "op": op, "dry": True})
        _print_json({"dry": True, "ok": True, "macro": mf.name,
                     "file": str(path), "actions": len(actions)})
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
