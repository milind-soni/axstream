"""Capture: turn a successful LLM-tier run into a reusable macro.

When the LLM tier executes a novel command, we have the utterance and the
concrete action stream that worked. To make it replayable for *variations*
of the command, we parameterize it: identify which action arguments came from
the utterance (the slots) and replace them with {placeholders}.

Slots are found by alignment — a span of the utterance that appears verbatim
as an action argument is almost certainly a slot (the "Standup" in "title it
Standup" shows up in both the command and the typed text). We ask a capable
model to do this alignment and name the template, which is far more robust
than string heuristics for messy speech, and it only runs once per new
command (offline, off the hot path).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

CAPTURE_SYSTEM = """You turn one successful voice command into a reusable macro template.

You are given the spoken UTTERANCE and the ACTIONS that were executed and
worked. Produce a template that will match future variations of this command.

Return JSON:
{
  "id": "snake_case_id",              // stable name, e.g. "new_note_titled"
  "description": "one line gloss",    // e.g. "open a new note with a title"
  "slots": ["title"],                 // the variable parts, [] if none
  "actions": [...],                   // the ACTIONS, but with slot values
                                      //   replaced by {slot} placeholders
  "examples": [                       // 1-2 phrasings that should match
    {"utterance": "...", "slots": {"title": "..."}}
  ]
}

Rules:
- A slot is a span of the UTTERANCE that appears as an action argument
  (a typed string, an app name, a message body). Replace it with {slot_name}
  in the actions AND record it in the example's slots.
- Fixed navigation (open app, keypress, wait) has no slots — keep it verbatim.
- If the command has no variable content, slots is [] and actions are unchanged.
- Output JSON only."""


def _validate(macro: dict) -> Optional[dict]:
    required = {"id", "description", "slots", "actions", "examples"}
    if not required.issubset(macro):
        return None
    if not isinstance(macro["id"], str) or not re.fullmatch(r"[a-z0-9_]+", macro["id"]):
        return None
    if not isinstance(macro["actions"], list) or not macro["actions"]:
        return None
    if not isinstance(macro["slots"], list):
        return None
    return macro


async def parameterize(
    utterance: str,
    actions: list[dict],
    stream_fn,
) -> Optional[dict]:
    """Ask the LLM tier's model to build a macro dict from a successful run.

    stream_fn(system, user) -> async iterator of text chunks (same interface
    the runner uses). Returns a macro dict, or None if parameterization failed.
    """
    user = (
        f"UTTERANCE: {utterance}\n\n"
        f"ACTIONS:\n{json.dumps(actions, indent=2)}\n\n"
        "Produce the macro template JSON."
    )
    text = ""
    async for chunk in stream_fn(CAPTURE_SYSTEM, user):
        text += chunk
    return _extract_json(text)


def _extract_json(text: str) -> Optional[dict]:
    # tolerate prose or code fences around the object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return _validate(json.loads(match.group(0)))
    except json.JSONDecodeError:
        return None


def infer_guard(actions: list[dict]) -> Optional[dict]:
    """A cheap default guard: the first ax-targeted action's element must
    resolve before we trust the replay. Slot-free targets make the best guards."""
    for op in actions:
        target = op.get("target")
        if isinstance(target, dict) and "ax" in target:
            ax = target["ax"]
            # prefer a guard with no slot placeholders (stable across runs)
            flat = json.dumps(ax)
            if "{" not in flat:
                return {"ax": ax}
    return None
