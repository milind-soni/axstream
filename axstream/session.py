"""Session — the integration façade: utterance text in, action out.

This is the seam for speech-to-text products (Wispr/Willow-style): the STT app
owns audio -> text; a Session owns text -> action. One call:

    from axstream import Session

    session = Session()
    await session.connect()
    result = await session.handle("launch safari")
    # {"tier": "instant", "template": "open_app", "slots": {"app": "safari"},
    #  "status": "done", "match_ms": 88, "total_ms": 1641}

`handle` runs the INSTANT tier: tiny-matcher match (~100ms, local) -> guarded
macro replay (no LLM). When nothing matches it returns {"tier": "none"} in
~100ms — route that to your own fallback (or axstream's LLM tier via
`runner.run_task`) and, on success, `learn` the run so it's instant next time.

Config (constructor args, env-overridable):
  matcher_url   AXSTREAM_TINY_URL   llama-server hosting the tiny matcher
  macros_path                       macro store (default ~/.axstream/macros.json)
  computer                          any Computer-shaped backend; default
                                    DriverComputer (cua-driver, background
                                    delivery), pass Computer(...) for
                                    computer-server or MockComputer for tests.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .ax import Snapshot
from .driver import DriverComputer
from .executor import Executor
from .macros import Macro, MacroStore
from .tiny import TinyMatcher


class Session:
    def __init__(self, matcher_url: Optional[str] = None,
                 macros_path: str = "~/.axstream/macros.json",
                 computer: Any = None, allow_risky: bool = False):
        self.store = MacroStore(path=macros_path)
        self.tiny = TinyMatcher(**({"url": matcher_url} if matcher_url else {}))
        self.computer = computer if computer is not None else DriverComputer()
        self.allow_risky = allow_risky

    async def connect(self) -> "Session":
        await self.computer.connect()
        if self.tiny.available():
            self.tiny.warm(self.store.templates())
        return self

    async def close(self) -> None:
        await self.computer.close()

    def ready(self) -> dict:
        """Health check for integrators: is the instant tier usable?"""
        return {
            "matcher": self.tiny.available(),
            "macros": len(self.store.macros),
        }

    async def handle(self, utterance: str) -> dict:
        """Instant tier for one utterance. Never raises on a failed action —
        the result's status says what happened (done / aborted / guard_failed /
        none) so the caller can decide whether to fall back."""
        t0 = time.perf_counter()
        utterance = utterance.strip()
        hit = self.tiny.match(utterance, self.store.templates()) if utterance else None
        match_ms = (time.perf_counter() - t0) * 1000
        if not hit:
            return {"tier": "none", "match_ms": round(match_ms), "total_ms": round(match_ms)}

        plan = self.store.resolve(hit["template"], hit.get("slots", {}))
        executor = Executor(self.computer, Snapshot({}), allow_risky=self.allow_risky)
        result = await executor.replay(plan["actions"], plan.get("guard"))
        return {
            "tier": "instant",
            "template": hit["template"],
            "slots": hit.get("slots", {}),
            "status": result.status,
            "reason": result.reason,
            "match_ms": round(match_ms),
            "total_ms": round((time.perf_counter() - t0) * 1000),
        }

    def learn(self, macro: Macro) -> None:
        """Add a macro (e.g. parameterized from a successful LLM-tier run via
        `capture.parameterize`) and re-warm the matcher's prompt prefix."""
        self.store.add(macro)
        if self.tiny.available():
            self.tiny.warm(self.store.templates())
