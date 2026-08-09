"""`axstream bench` — p50/p95 latency per op across repeated replays.

Warmup runs execute but don't count, absorbing cold app launches so
steady-state numbers are honest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from .geometry import annotate_window_relative
from .macrofile import MacroFileError, computer_for, load, resolve_name
from .replay import _print_json, run_actions


def cmd_bench(argv: list[str]) -> int:
    """`axstream bench <macro>` — replay a macro N times and report per-op
    and total latency (p50/p95/min/max)."""
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

    def pct(vals: list[float], p: float) -> int:
        s = sorted(vals)
        return int(s[min(len(s) - 1, int(round(p * (len(s) - 1))))])

    async def go() -> int:
        computer = computer_for(mf)
        if mf.extra.get("delivery") == "foreground": computer.delivery = "foreground"
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
