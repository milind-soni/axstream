"""The zoxide tier: a learned library of parameterized command macros.

A macro is a command the system has done before, stored as a template with
slots. The action stream is templated — slot placeholders like {title} are
filled from the utterance at replay time — so "title it standup" and "title it
yoyo" are the same macro with a different slot value.

Storage mirrors zoxide's db.zo: a small JSON file of entries, each ranked by
frecency (frequency x recency), so common commands win matches and stale ones
decay. Capture happens when the LLM tier succeeds at a novel command; a
big-model pass parameterizes that trajectory into a template (see capture.py).
Replay feeds the filled action stream to the same executor the LLM tier uses,
after checking a guard.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Recency multipliers, same shape as zoxide's score(): recent use counts more.
HOUR, DAY, WEEK = 3600, 86400, 604800


@dataclass
class Macro:
    id: str
    description: str  # natural-language gloss, shown to the tiny matcher
    slots: list[str]
    examples: list[dict]  # [{"utterance": str, "slots": {...}}]
    actions: list[dict]  # templated action ops; strings may contain {slot}
    guard: Optional[dict] = None  # an ax target that must resolve before replay
    app: Optional[str] = None  # app scope (launch name); None = universal
    rank: float = 1.0
    last_used: int = 0

    def score(self, now: int) -> float:
        dt = max(0, now - self.last_used)
        if dt < HOUR:
            return self.rank * 4.0
        if dt < DAY:
            return self.rank * 2.0
        if dt < WEEK:
            return self.rank * 0.5
        return self.rank * 0.25


def _fill(value: Any, slots: dict[str, str]) -> Any:
    """Substitute {slot} placeholders inside strings, recursively."""
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: slots.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _fill(v, slots) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, slots) for v in value]
    return value


class MacroStore:
    def __init__(self, path: str | Path = "~/.axstream/macros.json"):
        self.path = Path(path).expanduser()
        self.macros: dict[str, Macro] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.macros = {m["id"]: Macro(**m) for m in data}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(m) for m in self.macros.values()], indent=2))

    def templates(self) -> list[dict]:
        """The matcher's view: id + description + slots + examples, ranked so
        the most-used macros lead the prompt."""
        now = _now()
        ordered = sorted(self.macros.values(), key=lambda m: m.score(now), reverse=True)
        return [
            {"id": m.id, "description": m.description, "slots": m.slots, "examples": m.examples}
            for m in ordered
        ]

    def add(self, macro: Macro) -> None:
        existing = self.macros.get(macro.id)
        if existing:
            # merge: keep frecency, add any new example phrasings
            macro.rank = existing.rank
            seen = {e["utterance"] for e in existing.examples}
            macro.examples = existing.examples + [
                e for e in macro.examples if e["utterance"] not in seen
            ]
        self.macros[macro.id] = macro
        self.save()

    def resolve(self, template_id: str, slots: dict[str, str]) -> Optional[dict]:
        """Turn a matcher hit into a concrete, ready-to-run plan and bump its
        frecency. Returns {"actions": [...], "guard": {...}} or None."""
        macro = self.macros.get(template_id)
        if macro is None:
            return None
        macro.rank += 1.0
        macro.last_used = _now()
        self.save()
        return {
            "actions": _fill(macro.actions, slots),
            "guard": _fill(macro.guard, slots) if macro.guard else None,
        }


def _now() -> int:
    import time

    return int(time.time())
