"""App macro packs: generated in bulk, validated strictly, seeded as data.

Packs live as JSON arrays in axstream/packs/*.json. Generation is allowed to
be sloppy — validation here is the gate: anything that fails a check is
dropped (and reported), never seeded. The checks mirror the runtime contract,
so a macro that passes has a real chance of replaying.
"""

from __future__ import annotations

import json
import os
import re

from .macros import Macro

PACKS_DIR = os.path.join(os.path.dirname(__file__), "packs")

ALLOWED_DO = {"open", "key", "type", "wait"}
ALLOWED_KEYS = (
    {"cmd", "shift", "option", "ctrl", "fn", "return", "enter", "tab", "escape",
     "space", "delete", "home", "end", "pageup", "pagedown",
     "up", "down", "left", "right"}
    | set("abcdefghijklmnopqrstuvwxyz0123456789")
    | {f"f{i}" for i in range(1, 13)}
)


def _check(m: dict) -> list[str]:
    errs: list[str] = []
    if not re.fullmatch(r"[a-z0-9_]+", m.get("id") or ""):
        errs.append("bad id")
    if not m.get("description"):
        errs.append("no description")
    slots = m.get("slots")
    if not isinstance(slots, list):
        errs.append("slots not a list")
        slots = []
    if not isinstance(m.get("examples"), list) or not m["examples"]:
        errs.append("no examples")
    for ex in m.get("examples") or []:
        utt = (ex.get("utterance") or "").lower()
        ex_slots = ex.get("slots") or {}
        if set(ex_slots) != set(slots):
            errs.append(f"example slots {set(ex_slots)} != {set(slots)}")
        for v in ex_slots.values():
            if str(v).lower() not in utt:
                errs.append(f"slot value {v!r} not verbatim in utterance")
    actions = m.get("actions")
    if not isinstance(actions, list) or not actions:
        errs.append("no actions")
    used_slots: set[str] = set()
    for op in actions or []:
        do = op.get("do")
        if op.get("op") != "act" or do not in ALLOWED_DO:
            errs.append(f"bad op {op}")
            continue
        if do == "key":
            for k in op.get("keys") or []:
                if str(k).lower() not in ALLOWED_KEYS:
                    errs.append(f"bad key {k!r}")
        if do == "wait" and not isinstance(op.get("ms"), int):
            errs.append("wait without int ms")
        for field in ("text", "target"):
            val = op.get(field)
            if isinstance(val, str):
                used_slots.update(re.findall(r"\{(\w+)\}", val))
    if used_slots - set(slots):
        errs.append(f"undeclared placeholders {used_slots - set(slots)}")
    return errs


def load_packs(verbose: bool = False) -> tuple[list[Macro], list[str]]:
    """Returns (valid macros, human-readable rejects)."""
    macros: list[Macro] = []
    rejects: list[str] = []
    seen: set[str] = set()
    if not os.path.isdir(PACKS_DIR):
        return macros, rejects
    for name in sorted(os.listdir(PACKS_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            rows = json.load(open(os.path.join(PACKS_DIR, name)))
        except json.JSONDecodeError as e:
            rejects.append(f"{name}: unreadable ({e})")
            continue
        for m in rows if isinstance(rows, list) else []:
            errs = _check(m) if isinstance(m, dict) else ["not an object"]
            mid = m.get("id", "?") if isinstance(m, dict) else "?"
            if mid in seen:
                errs.append("duplicate id")
            if errs:
                rejects.append(f"{name}:{mid}: " + "; ".join(errs))
                continue
            seen.add(mid)
            macros.append(Macro(id=m["id"], description=m["description"],
                                slots=m["slots"], examples=m["examples"],
                                actions=m["actions"], guard=m.get("guard"),
                                app=m.get("app")))
    if verbose and rejects:
        for r in rejects:
            print(f"  [pack reject] {r}")
    return macros, rejects
