"""Tiny local matcher: intent + slot extraction against the macro library.

Runs a sub-1B model (LFM2.5-350M) under llama.cpp's llama-server with
JSON-schema constrained decoding, so the output is grammatically guaranteed
to be a valid {template, slots} object whose template is one of the stored
ids (or "none"). ~60-120ms per call on Apple Silicon; the system prompt is a
stable prefix, so llama-server's prefix cache keeps prefill near-free.

The matcher's contract is deliberately conservative: when unsure it answers
"none", which routes the utterance to the LLM tier. A wrong "none" costs one
slow call; a wrong match could perform a wrong action — so the fine-tune and
few-shot examples optimize recall, while the schema guarantees validity.

Use a FINE-TUNED matcher model: the base LFM2.5-350M scores ~47% end-to-end on
this task; a small LoRA fine-tune reaches ~93% at the same ~100ms latency.
Point AXSTREAM_TINY_URL (or the url arg) at whichever llama-server hosts it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

DEFAULT_URL = os.environ.get("AXSTREAM_TINY_URL", "http://localhost:8791")


def build_schema(templates: list[dict]) -> dict:
    """One oneOf branch per template, each tying the template id to exactly
    its own slot keys — wrong-slot outputs are grammatically impossible."""
    branches: list[dict] = []
    for t in templates:
        slot_props = {name: {"type": "string"} for name in t.get("slots", [])}
        branches.append({
            "type": "object",
            "properties": {
                "template": {"const": t["id"]},
                "slots": {
                    "type": "object",
                    "properties": slot_props,
                    "required": list(slot_props),
                    "additionalProperties": False,
                },
            },
            "required": ["template", "slots"],
            "additionalProperties": False,
        })
    branches.append({
        "type": "object",
        "properties": {
            "template": {"const": "none"},
            "slots": {"type": "object", "additionalProperties": False},
        },
        "required": ["template", "slots"],
        "additionalProperties": False,
    })
    return {"oneOf": branches}


def build_prompt(templates: list[dict]) -> str:
    lines = [
        "Match the user's spoken command to ONE known template and extract its "
        "slot values exactly as spoken.",
        "",
        "TEMPLATES:",
    ]
    for t in templates:
        slot_desc = ", ".join(t.get("slots", [])) or "no slots"
        lines.append(f"- {t['id']} — {t['description']} (slots: {slot_desc})")
        for example in t.get("examples", [])[:3]:
            expected = json.dumps({"template": t["id"], "slots": example.get("slots", {})})
            lines.append(f'  Example: "{example["utterance"]}" -> {expected}')
    lines += [
        '- none — the command fits NO template above. Example: "what time is it" '
        '-> {"template":"none","slots":{}}',
        "",
        "Rules: slot values are copied verbatim from the command. If unsure, "
        'use "none". Output JSON only.',
    ]
    return "\n".join(lines)


def slots_verbatim(utterance: str, slots: dict) -> bool:
    """The matcher's contract: every slot value is copied verbatim from the
    utterance. A value that isn't there was hallucinated (typically copied from
    a few-shot example) — reject the match rather than act on invented input."""
    low = utterance.lower()
    return all(str(v).strip().lower() in low for v in slots.values())


class TinyMatcher:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 5.0):
        self.url = url
        self._client = httpx.Client(timeout=timeout)

    def available(self) -> bool:
        try:
            return self._client.get(f"{self.url}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def match(self, utterance: str, templates: list[dict]) -> Optional[dict[str, Any]]:
        """Returns {"template": id, "slots": {...}} or None for no-match/error."""
        if not templates:
            return None
        payload = {
            "messages": [
                {"role": "system", "content": build_prompt(templates)},
                {"role": "user", "content": utterance},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": build_schema(templates)},
            },
            "max_tokens": 120,
            "temperature": 0,
        }
        try:
            r = self._client.post(f"{self.url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            result = json.loads(r.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, KeyError, json.JSONDecodeError):
            return None
        if result.get("template") in (None, "none"):
            return None
        if not slots_verbatim(utterance, result.get("slots", {})):
            return None  # hallucinated slot -> route to the LLM tier instead
        return result

    def warm(self, templates: list[dict]) -> None:
        """Burn the first-call graph compile + cache the system-prompt prefix."""
        try:
            self.match("warm up", templates or [
                {"id": "warmup", "description": "warm", "slots": [], "examples": []}
            ])
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            pass
