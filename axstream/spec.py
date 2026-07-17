"""axstream action spec v0.

The wire format is JSONL: one self-contained JSON object per line. A line is
either executed atomically or not at all -- the newline is the commit signal.
Lines stream inside a ```spec fence; prose outside the fence is narration.

Ops:
  {"op":"act","do":"click","target":{"ax":{"id":"e12"}}}
  {"op":"act","do":"click","target":{"x":420,"y":312},"risk":"risky"}
  {"op":"act","do":"type","text":"hello"}
  {"op":"act","do":"key","keys":["cmd","s"]}
  {"op":"act","do":"scroll","direction":"down","clicks":3}
  {"op":"act","do":"move","target":{...}}
  {"op":"act","do":"open","target":"Safari"}          # app name or URL
  {"op":"act","do":"wait","ms":300}
  {"op":"assert","target":{"ax":{"role":"AXButton","title":"Save"}}}
  {"op":"observe"}                                    # end burst, request fresh look
  {"op":"done","status":"success","reason":"..."}

Targets: {"ax": {"id": "e12"}} references an element from the observation
summary; {"ax": {"role": ..., "title": ...}} fuzzy-resolves against the live
tree at execution time (late binding); {"x":..,"y":..} is a raw coordinate.
"""

from __future__ import annotations

from typing import Any

# do-name -> (required fields, optional fields, default risk)
ACTIONS: dict[str, tuple[set[str], set[str], str]] = {
    "click": ({"target"}, {"risk"}, "safe"),
    "double_click": ({"target"}, {"risk"}, "safe"),
    "type": ({"text"}, {"risk"}, "safe"),
    "key": ({"keys"}, {"risk"}, "safe"),
    "scroll": ({"direction"}, {"clicks", "risk"}, "safe"),
    "move": ({"target"}, {"risk"}, "safe"),
    "open": ({"target"}, {"risk"}, "safe"),
    "wait": ({"ms"}, set(), "safe"),
}

OPS = {"act", "assert", "observe", "done"}


def validate_op(obj: Any) -> tuple[bool, str]:
    """Cheap structural validation of a parsed line. Returns (ok, error)."""
    if not isinstance(obj, dict):
        return False, "not an object"
    op = obj.get("op")
    if op not in OPS:
        return False, f"unknown op: {op!r}"
    if op == "act":
        do = obj.get("do")
        if do not in ACTIONS:
            return False, f"unknown action: {do!r}"
        required, optional, _ = ACTIONS[do]
        missing = required - obj.keys()
        if missing:
            return False, f"{do}: missing {sorted(missing)}"
        if "target" in required and not _valid_target(obj["target"], do):
            return False, f"{do}: bad target {obj['target']!r}"
    if op == "assert" and not _valid_target(obj.get("target"), "assert"):
        return False, f"assert: bad target {obj.get('target')!r}"
    if op == "done" and obj.get("status") not in ("success", "failure"):
        return False, f"done: bad status {obj.get('status')!r}"
    return True, ""


def _valid_target(target: Any, do: str) -> bool:
    if do == "open":
        return isinstance(target, str) and bool(target)
    if not isinstance(target, dict):
        return False
    if "x" in target and "y" in target:
        return isinstance(target["x"], (int, float)) and isinstance(target["y"], (int, float))
    ax = target.get("ax")
    if isinstance(ax, dict):
        return bool(ax.get("id") or ax.get("role") or ax.get("title"))
    return False


def risk_of(op: dict) -> str:
    if op.get("op") != "act":
        return "safe"
    default = ACTIONS.get(op.get("do"), (set(), set(), "safe"))[2]
    return op.get("risk", default)
