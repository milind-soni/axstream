"""The verify-before-store gate + task-family dedup (PreAct, arXiv 2606.17929).

PreAct's one structural finding: caching replayable programs WITHOUT a
verification gate collapses (their gate was worth 1.75-2.6 tasks on every
platform; ungated Workflow-Use scored zero warm). Their failure class —
"lossy" programs that replay to full coverage, report success, and solve
nothing — is one we hit ourselves the same day (a Notes macro reported 11/11
while typing into the wrong note). The rule this module enforces:

    store the program you run, and never keep one you have not checked.

Two pieces:

* verify(mf): replay the macro live ONCE and require its TERMINAL ASSERT to
  pass (the outcome evaluator — a macro without a terminal assert cannot be
  verified, only stored unverified). On pass, a `verified` stamp — content
  hash + timestamp + blind-step count — is written into the header; editing
  the actions invalidates the stamp (hash mismatch = stale). PreAct's L6
  caveat is enforced, not just documented: verification RE-EXECUTES the
  macro, so anything carrying a `risk:"risky"` op refuses to auto-verify —
  irreversible tasks get verified by a human, not a replay loop.

* signature(mf): the task-family dedup key — a hash of the macro's action
  SHAPE (ops, targets, key chords) with typed values and slot fills blanked.
  Two macros that do the same thing under different names collide here, and
  the store upserts: the superseded file moves to .archive/ instead of
  rotting alongside its twin (observed live: safari_new_tab vs
  open_safari_new_tab). PreAct calls this compile-extend-REPLACE — a corpus
  that mutates, not an append-only pile.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re as _re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .macrofile import MacroFile, discover, dumps, load, save
from .spec import risk_of

USER_DIR = Path("~/.axstream/macros").expanduser()
ARCHIVE_DIR_NAME = ".archive"


# -- task-family signature -------------------------------------------------


def _shape(op: dict) -> Optional[tuple]:
    """One op's contribution to the task-family signature. Values that vary
    per run (typed text, slot fills, waits) are blanked; structure that
    defines WHAT the macro does (apps, chords, click targets) is kept."""
    kind = op.get("op")
    if kind == "act":
        do = op.get("do")
        target = op.get("target")
        if do == "open":
            return ("open", str(target).strip().lower())
        if do in ("click", "double_click"):
            label = ""
            if isinstance(target, dict):
                ax = target.get("ax") if isinstance(target.get("ax"), dict) else {}
                label = (target.get("text") or ax.get("title") or "").strip().lower()
            return (do, label or "px")
        if do == "type":
            # Slot placeholders are parameterization (blank them); LITERAL
            # typed text is task identity — "type a search query" and "type
            # this exact URL" are different tasks even with identical
            # surrounding keystrokes (observed: a nav macro upserted away a
            # search macro). Hash literals so instances stay distinct without
            # storing content in the signature.
            text = str(op.get("text") or "")
            slots = tuple(sorted(set(_re.findall(r"\{(\w+)\}", text))))
            if slots:
                return ("type", "slots", slots)
            digest = hashlib.sha1(text.encode()).hexdigest()[:8]
            return ("type", "lit", digest)
        if do == "key":
            keys = op.get("keys") or []
            return ("key", "+".join(str(k).lower() for k in keys))
        if do == "scroll":
            return ("scroll", str(op.get("direction", "")).lower())
        if do == "wait":
            return None  # timing noise, not task identity
        return (do,)
    if kind == "assert":
        return ("assert",)
    if kind == "done":
        return None
    return (kind,)


def signature(mf: MacroFile) -> str:
    shapes = [s for s in (_shape(op) for op in mf.actions) if s is not None]
    blob = json.dumps(shapes, ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def actions_hash(mf: MacroFile) -> str:
    blob = json.dumps(mf.actions, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def slot_value_hash(value: object) -> str:
    """Privacy-preserving marker for first-run capture values."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


# -- gate predicates -------------------------------------------------------


def terminal_assert(mf: MacroFile) -> bool:
    """True when the macro ENDS with an outcome check: an assert at, or
    after, the last act op (a trailing `done` is fine)."""
    last_assert = last_act = -1
    for i, op in enumerate(mf.actions):
        if op.get("op") == "assert":
            last_assert = i
        elif op.get("op") == "act":
            last_act = i
    return last_assert > last_act >= 0 or (last_assert >= 0 and last_act == -1)


def risky_ops(mf: MacroFile) -> list[int]:
    return [i for i, op in enumerate(mf.actions) if risk_of(op) == "risky"]


def verification(mf: MacroFile) -> dict:
    """The macro's CURRENT verification status (stamp checked against the
    live content hash — edits after verification demote to 'stale')."""
    stamp = mf.extra.get("verified")
    if not isinstance(stamp, dict):
        return {"state": "unverified"}
    if stamp.get("hash") != actions_hash(mf):
        return {"state": "stale", "verified_at": stamp.get("at")}
    return {"state": "verified", "verified_at": stamp.get("at"),
            "blind_steps": stamp.get("blind_steps", 0)}


# -- the live gate ---------------------------------------------------------


async def _replay_once(actions: list[dict], emit, delivery=None) -> int:
    from .driver import DriverComputer
    from .replay import run_actions

    computer = DriverComputer()
    computer.delivery = delivery
    await computer.connect()
    await computer.fast_cursor()
    try:
        return await run_actions(actions, computer, emit=emit)
    finally:
        await computer.close()


def verify(name_or_path: str, slots: Optional[dict] = None,
           emit=lambda _line: None) -> dict:
    """Run the verify gate on one macro: live replay + terminal assert.
    Returns {ok, reason?, ...}; on success the stamp is saved into the file.

    The caller decides WHEN this is safe to run — a verify replay executes
    the task for real (PreAct L6): side-effect-safe macros only."""
    from .geometry import annotate_window_relative
    from .macrofile import MacroFileError, resolve_name

    path = resolve_name(name_or_path)
    if path is None:
        return {"ok": False, "reason": f"no macro named {name_or_path!r}"}
    try:
        mf = load(path)
    except MacroFileError as e:
        return {"ok": False, "reason": str(e)}

    if not terminal_assert(mf):
        return {"ok": False, "reason":
                "no terminal assert — the macro cannot prove its outcome. "
                "Add a final {\"op\":\"assert\",\"target\":{\"text\":...}} "
                "that only passes when the task actually worked."}
    risky = risky_ops(mf)
    if risky:
        return {"ok": False, "reason":
                f"op(s) {risky} are risk:\"risky\" — a verify replay "
                "re-executes the task, which is unsafe for irreversible "
                "actions. Verify this macro manually."}

    fill = dict(slots or {})
    for slot_name, spec in (mf.slots or {}).items():
        if slot_name not in fill:
            example = spec.get("example") if isinstance(spec, dict) else None
            if example in (None, ""):
                return {"ok": False, "reason":
                        f"slot {slot_name!r} has no value and no header "
                        "example — pass slots or add an example"}
            fill[slot_name] = str(example)
    try:
        actions = mf.fill(fill)
    except MacroFileError as e:
        return {"ok": False, "reason": str(e)}
    captured_hashes = mf.provenance.get("captured_slot_hashes")
    if isinstance(captured_hashes, dict):
        reused = sorted(
            name for name, digest in captured_hashes.items()
            if name in fill and slot_value_hash(fill[name]) == digest
        )
        if reused:
            return {"ok": False, "reason":
                    "verification must use fresh slot value(s), not the "
                    f"captured first-run value: {', '.join(reused)}. "
                    "A distinct value proves the terminal assertion did not "
                    "pass on stale first-run UI."}
    recorded_window = mf.extra.get("window")
    if isinstance(recorded_window, dict):
        actions = annotate_window_relative(actions, recorded_window)

    lines: list[dict] = []

    def collect(line: dict) -> None:
        lines.append(line)
        emit(line)

    delivery = mf.extra.get("delivery")
    code = asyncio.run(_replay_once(actions, collect, delivery=delivery))
    escalated = False
    if code != 0 and delivery is None:
        # Delivery auto-discovery: Blender-class OpenGL/custom-event-loop apps
        # silently ignore background pid-addressed input, so the replay no-ops
        # and the assert fails. The gate is the one place a re-run is EXPECTED,
        # so retry once with escalated foreground delivery; if that passes,
        # the macro learns its delivery mode permanently.
        lines.clear()
        code = asyncio.run(_replay_once(actions, collect, delivery="foreground"))
        escalated = code == 0
    if code != 0:
        last = lines[-1] if lines else {}
        return {"ok": False, "reason": f"live replay failed: "
                f"{last.get('reason', 'see progress lines')}",
                "failed_at": last.get("failed_at")}
    summary = lines[-1] if lines else {}
    blind = len(summary.get("unverified_steps") or [])
    if escalated:
        mf.extra["delivery"] = "foreground"
    mf.extra["verified"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "hash": actions_hash(mf), "blind_steps": blind}
    save(mf, path)
    return {"ok": True, "file": str(path), "blind_steps": blind,
            **({"delivery": "foreground",
                "note_delivery": "app ignores background input; foreground "
                "delivery saved into the macro (replays briefly front its "
                "window)"} if escalated else {}),
            "note": "verified: one live replay succeeded and the terminal "
                    "assert passed" + (f" ({blind} blind step(s) remain — "
                                       "consider better anchors)" if blind else "")}


# -- upsert-by-signature ---------------------------------------------------


def upsert_conflicts(mf: MacroFile, exclude_stem: str) -> list[Path]:
    """Files in the user macro dir whose task-family signature matches
    `mf`'s — the near-dupes an upsert should supersede."""
    sig = signature(mf)
    out = []
    for path, other in discover([USER_DIR]):
        if isinstance(other, MacroFile) and path.stem != exclude_stem \
                and signature(other) == sig:
            out.append(path)
    return out


def archive(path: Path) -> Path:
    """Move a superseded macro into .archive/ (never silently delete a
    user's file — but never leave twins rotting side by side either)."""
    dest_dir = path.parent / ARCHIVE_DIR_NAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{path.stem}.{n}{path.suffix}"
        n += 1
    path.rename(dest)
    return dest


def cmd_verify(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="axstream verify",
        description="Verify-before-trust gate: one live replay + the "
                    "terminal assert must pass; stamps the macro on success.")
    parser.add_argument("target", help="macro name or file path")
    parser.add_argument("--slots", default=None, help="slot values as JSON")
    args = parser.parse_args(argv)
    slots = None
    if args.slots:
        try:
            slots = json.loads(args.slots)
        except ValueError as e:
            print(json.dumps({"error": f"--slots must be JSON: {e}"}))
            return 2
    result = verify(args.target, slots,
                    emit=lambda line: print(json.dumps(line, ensure_ascii=False),
                                            flush=True))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1
