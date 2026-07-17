"""Newline-committed stream compiler.

Adapted from json-render's createSpecStreamCompiler, with one deliberate
divergence: NO dedup of identical lines. json-render dedupes because RFC 6902
patches are idempotent; actions are not -- clicking the same button twice is a
legitimate plan. Ordering and at-most-once emission per physical line are
guaranteed by the newline framing itself.

push() accepts raw text chunks from any LLM stream and yields events:
  ("action", op_dict)   -- a validated op, ready to execute
  ("text", line)        -- narration outside the ```spec fence
  ("invalid", line, err)-- a fence line that failed parse/validation
"""

from __future__ import annotations

import json
from typing import Iterator

from .spec import validate_op

FENCE_CLOSE = "```"

Event = tuple  # ("action", dict) | ("text", str) | ("invalid", str, str)


class StreamCompiler:
    def __init__(self, fenced: bool = True):
        self._buffer = ""
        self._in_fence = not fenced  # unfenced mode treats the whole stream as spec lines
        self._fenced = fenced

    def push(self, chunk: str) -> Iterator[Event]:
        self._buffer += chunk
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()  # keep the incomplete tail buffered
        for line in lines:
            yield from self._line(line)

    def finish(self) -> Iterator[Event]:
        """Flush the trailing buffered line at end of stream."""
        if self._buffer.strip():
            yield from self._line(self._buffer)
        self._buffer = ""

    def _line(self, line: str) -> Iterator[Event]:
        stripped = line.strip()
        if not stripped:
            return
        if self._fenced:
            # be liberal in what we accept: models label the fence ```spec,
            # ```jsonl, ```json, or nothing at all
            if not self._in_fence and stripped.startswith("```"):
                self._in_fence = True
                return
            if stripped == FENCE_CLOSE and self._in_fence:
                self._in_fence = False
                return
        if not self._in_fence:
            yield ("text", stripped)
            return
        if not stripped.startswith("{"):
            yield ("invalid", stripped, "not a JSON object")
            return
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as e:
            yield ("invalid", stripped, f"parse error: {e}")
            return
        ok, err = validate_op(obj)
        if ok:
            yield ("action", obj)
        else:
            yield ("invalid", stripped, err)
