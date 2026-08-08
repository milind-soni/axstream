"""The receipts ledger: every replay leaves one line of evidence.

Value that is invisible doesn't exist. A replay that saved four minutes of
agent looping looks like nothing happened — so each run appends one JSON line
to ~/.axstream/runs.jsonl (macro, outcome, ms, rungs used), and
`axstream stats` turns the file into the number that makes the flywheel
visible: time saved versus doing the task through a reasoning loop.

The baseline for "saved" is deliberately conservative: 90s per successful
replay — the LOW end of measured warm native computer-use runs for the same
tasks (117.6s Notes, 177.3s cross-app), minus our own run time.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

LEDGER = Path("~/.axstream/runs.jsonl").expanduser()
BASELINE_MS = 90_000  # conservative: measured warm agent-loop runs were 117-287s


def record(name: str, code: int, ms: int, events: list[dict]) -> None:
    """Append one run receipt. Never raises — a full disk must not fail a
    replay that already succeeded."""
    try:
        vias = [e.get("via") for e in events if isinstance(e, dict) and e.get("via")]
        line = {"at": int(time.time()), "macro": name, "ok": code == 0,
                "ms": ms, "vias": vias}
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def cmd_stats(argv: list[str]) -> int:
    """`axstream stats` — the flywheel, as a number."""
    if not LEDGER.exists():
        print("no runs recorded yet — replay something first")
        return 0
    runs = []
    for l in LEDGER.read_text().splitlines():
        if not l.strip():
            continue
        try:
            r = json.loads(l)
            # legacy launcher-era lines used string timestamps / other keys —
            # coerce what we can, skip what we can't, never crash on history
            r["at"] = float(r.get("at") or 0)
            r["ms"] = int(r.get("ms") or 0)
            runs.append(r)
        except (ValueError, TypeError):
            continue
    ok = [r for r in runs if r.get("ok")]
    week = [r for r in ok if r["at"] > time.time() - 7 * 86400]
    saved_ms = sum(max(0, BASELINE_MS - r["ms"]) for r in ok)
    week_ms = sum(max(0, BASELINE_MS - r["ms"]) for r in week)
    by: dict[str, int] = {}
    for r in ok:
        by[r["macro"]] = by.get(r["macro"], 0) + 1
    top = sorted(by.items(), key=lambda kv: -kv[1])[:5]
    print(f"  replays: {len(ok)} ok / {len(runs)} total"
          f"   ·   time saved ~{saved_ms/60000:.0f} min all-time,"
          f" ~{week_ms/60000:.0f} min this week")
    for name, n in top:
        print(f"    {n:4}×  {name}")
    print("  (baseline: 90s/task — the low end of measured agent-loop runs)")
    return 0
