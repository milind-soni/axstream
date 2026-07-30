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


def debind(actions: list[dict]) -> list[dict]:
    """Rewrite per-burst element-id targets to late-bound role/title targets.
    Snapshot ids (e4) are meaningless outside the burst that created them; a
    macro that stores them can never replay. The executor records what an id
    resolved to — bake that in as the durable target."""
    out: list[dict] = []
    for op in actions:
        target = op.get("target")
        if (isinstance(target, dict) and isinstance(target.get("ax"), dict)
                and target["ax"].get("id")):
            ax = None
            r = op.get("resolved_ax")
            if isinstance(r, dict) and (r.get("role") or r.get("title")):
                ax = {k: v for k, v in r.items() if v}
            else:
                m = re.match(r"(\S+) '(.*)'$", op.get("resolved", ""))
                if m:
                    ax = {"role": m.group(1), "title": m.group(2)}
            if ax:
                op = {**op, "target": {"ax": ax}}
        out.append({k: v for k, v in op.items()
                    if k not in ("resolved", "resolved_ax")})
    return out


def _norm_text(s: str) -> str:
    """Compare labels and typed text ignoring case, spacing and punctuation —
    a recorder samples an element's label MID-KEYSTROKE, so the captured label
    is often a truncated, space-mangled prefix of what was typed."""
    return "".join(c for c in s.lower() if c.isalnum())


def content_derived(label: str, typed: list[str], min_len: int = 8) -> bool:
    """Is this label just the content the user typed (not a durable selector)?

    A recording of "type X, then click the text you just typed" captures the
    TYPED CONTENT as the element's label. Replayed with a different slot value
    that label can never match, so every such click silently falls through to
    stale coordinates. Detect it by prefix-overlap against the run's typed
    text, in either direction (the label may be a truncation of the text, or
    the text a continuation of the label).
    """
    a = _norm_text(label)
    if len(a) < min_len:
        return False  # short labels ("Save", "OK") are real selectors
    for text in typed:
        b = _norm_text(text)
        if not b:
            continue
        head = min(len(a), len(b), 12)  # compare the stable leading run
        if head >= min_len and a[:head] == b[:head]:
            return True
        if a in b or b in a:
            return True
    return False


def sanitize(actions: list[dict],
             slot_examples: Optional[list[str]] = None) -> tuple[list[dict], list[str]]:
    """Scrub a freshly recorded action list of selectors that cannot replay.

    Returns (actions, notes). Today it drops content-derived `ax` labels,
    leaving the op's recorded coordinates as its only target so the pixel rung
    runs deliberately instead of after a guaranteed-miss AX lookup (an op with
    no coordinates keeps its label — a doomed lookup beats no target at all).
    """
    typed = [op["text"] for op in actions
             if op.get("do") == "type" and isinstance(op.get("text"), str)]
    # A parameterized macro types "{slot}", so the literal text that produced
    # the recorded labels is gone from the actions — but the slot's EXAMPLE is
    # exactly that original text. Without it, every content-derived label on a
    # slotted macro survives (the case that made replay hit-or-miss).
    typed += list(slot_examples or [])
    notes: list[str] = []
    out: list[dict] = []
    for op in actions:
        target = op.get("target")
        if isinstance(target, dict) and isinstance(target.get("ax"), dict):
            label = target["ax"].get("title") or ""
            has_coords = "x" in target and "y" in target
            if label and has_coords and content_derived(label, typed):
                target = {k: v for k, v in target.items() if k != "ax"}
                op = {**op, "target": target}
                notes.append(f"dropped content-derived label {label!r} "
                             f"from {op.get('do')} (kept coordinates)")
        out.append(op)
    return out, notes


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
