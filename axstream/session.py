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

import os
import re
import time
from typing import Any, Callable, Optional

from .ax import Snapshot
from .capture import debind, infer_guard, parameterize
from .driver import DriverComputer
from .executor import Executor
from .llm import stream_openai_compat
from .macros import Macro, MacroStore
from .runner import run_task
from .tiny import TinyMatcher


def _default_llm() -> Optional[Callable]:
    """Pick the fast-tier model from the env: OpenRouter first (paid, no
    free-tier rate crawl), then Groq. None disables the fast tier."""
    or_key = os.environ.get("OPENROUTER_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")
    if or_key:
        def factory(system: str, user: str):
            return stream_openai_compat(
                system, user, model="qwen/qwen3.6-27b", api_key=or_key,
                base_url="https://openrouter.ai/api/v1",
                extra={"reasoning": {"enabled": False}},
            )
        return factory
    if groq_key:
        def factory(system: str, user: str):
            return stream_openai_compat(
                system, user, model="qwen/qwen3.6-27b", api_key=groq_key,
                base_url="https://api.groq.com/openai/v1",
                extra={"reasoning_effort": "none"},
            )
        return factory
    return None


_FILLERS = {"okay", "ok", "so", "um", "uh", "hmm", "yeah", "yes", "no", "hey",
            "hi", "hello", "thanks", "thank", "you", "well", "right", "cool",
            "like", "just", "please", "can", "could", "the", "a", "and", "then"}


def _looks_like_command(utterance: str) -> bool:
    """Cheap gate before waking the LLM tier: fragments and small talk
    ("okay, so", "hmm", "hello") must not reach a planner that owns the mouse.
    Anything with real content words passes; the planner still decides."""
    words = re.findall(r"[a-z0-9'.]+", utterance.lower())
    content = [w for w in words if w not in _FILLERS]
    return len(content) >= 2 or (len(content) == 1 and len(words) <= 2)


def _op_line(op: dict) -> str:
    """One terse line per spec action, placeholders and all."""
    do = op.get("do", op.get("op"))
    target = op.get("target")
    if isinstance(target, dict) and "ax" in target:
        ax = target["ax"]
        target = f"{ax.get('role', '')} {ax.get('title') or ax.get('id') or ''!r}".strip()
    detail = target or op.get("text") or op.get("keys") or op.get("ms") or ""
    return f"{do} {detail}".strip()


def _replay_printer(e: dict) -> None:
    kind = e.get("kind")
    if kind == "executed":
        ms = (e["t_done"] - e["t_start"]) * 1000
        print(f"    > {_op_line(e['op'])}  ({ms:.0f}ms)")
    elif kind in ("guard_failed", "action_failed"):
        print(f"    X {kind}: {e.get('error', e.get('guard', ''))}")


class Session:
    def __init__(self, matcher_url: Optional[str] = None,
                 macros_path: str = "~/.axstream/macros.json",
                 computer: Any = None, allow_risky: bool = False,
                 llm: Optional[Callable] = None, max_bursts: int = 4,
                 verbose: bool = False):
        self.store = MacroStore(path=macros_path)
        self.tiny = TinyMatcher(**({"url": matcher_url} if matcher_url else {}))
        self.computer = computer if computer is not None else DriverComputer()
        self.allow_risky = allow_risky
        self.llm = llm if llm is not None else _default_llm()
        self.max_bursts = max_bursts
        self.verbose = verbose  # stream per-action logs to stdout as things run
        self._pending: set = set()  # background learn tasks
        self._frontmost: Optional[str] = None  # cached frontmost app name

    async def connect(self) -> "Session":
        await self.computer.connect()
        await self._refresh_frontmost()
        if self.tiny.available():
            self.tiny.warm(self._library())
        return self

    async def close(self) -> None:
        if self._pending:  # let in-flight background learning persist first
            import asyncio
            await asyncio.gather(*self._pending, return_exceptions=True)
        await self.computer.close()

    async def _refresh_frontmost(self) -> None:
        """Best-effort cache of the frontmost app name (drives app-scoping)."""
        if not hasattr(self.computer, "tool"):
            return
        try:
            apps = await self.computer.tool("list_apps")
            active = [a for a in apps.get("apps", [])
                      if a.get("active") and a.get("running")]
            if active:
                self._frontmost = active[0].get("name")
        except Exception:  # noqa: BLE001 - scoping is an optimization
            pass

    def _library(self, utterance: str = "") -> list[dict]:
        """The matcher's view, capped near its trained library size. Selection
        blends three signals so a 200-macro store still fits in a 25-slot
        prompt: keyword overlap with the utterance (retrieval), frontmost-app
        scope, and frecency. Colder misses fall to the LLM tier."""
        ranked = self.store.templates()
        by_id = {m.id: m for m in self.store.macros.values()}
        front = (self._frontmost or "").lower()
        words = {w for w in re.findall(r"[a-z0-9']+", utterance.lower())
                 if len(w) > 2}

        def overlap(t: dict) -> int:
            hay: set[str] = set(re.findall(r"[a-z0-9']+", t["description"].lower()))
            for ex in t.get("examples", []):
                hay.update(re.findall(r"[a-z0-9']+", ex["utterance"].lower()))
            return len(words & hay)

        def key(item: tuple[int, dict]) -> tuple:
            idx, t = item
            app = (by_id[t["id"]].app or "") if t["id"] in by_id else ""
            scoped = bool(app) and bool(front) and app.lower() == front
            return (-overlap(t), 0 if scoped else (1 if not app else 2), idx)

        ordered = [t for _, t in sorted(enumerate(ranked), key=key)]
        return ordered[:25]

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
        hit = self.tiny.match(utterance, self._library(utterance)) if utterance else None
        match_ms = (time.perf_counter() - t0) * 1000
        if not hit:
            if self.llm and utterance and _looks_like_command(utterance):
                return await self._fast_tier(utterance, t0, match_ms)
            return {"tier": "none", "match_ms": round(match_ms), "total_ms": round(match_ms)}

        plan = self.store.resolve(hit["template"], hit.get("slots", {}))
        if self.verbose:
            print(f"  template [{hit['template']}] slots={hit.get('slots', {})}")
            for op in plan["actions"]:
                print(f"    {_op_line(op)}")
        executor = Executor(self.computer, Snapshot({}), allow_risky=self.allow_risky,
                            on_event=_replay_printer if self.verbose else None)
        result = await executor.replay(plan["actions"], plan.get("guard"))
        import asyncio
        refresh = asyncio.create_task(self._refresh_frontmost())
        self._pending.add(refresh)
        refresh.add_done_callback(self._pending.discard)
        if result.status in ("aborted", "guard_failed") and self.llm:
            # the replay hit reality and lost (unknown app name, UI drift) —
            # this is exactly what the LLM tier is for; its success re-learns
            if self.verbose:
                print(f"  replay {result.status} ({result.reason}) — "
                      "falling back to the LLM tier")
            return await self._fast_tier(utterance, t0, match_ms)
        return {
            "tier": "instant",
            "template": hit["template"],
            "slots": hit.get("slots", {}),
            "status": result.status,
            "reason": result.reason,
            "match_ms": round(match_ms),
            "total_ms": round((time.perf_counter() - t0) * 1000),
        }

    async def _fast_tier(self, utterance: str, t0: float, match_ms: float) -> dict:
        """No macro matched: let the LLM plan and execute over the live screen,
        then capture the success as a macro so next time is instant."""
        executed: list[dict] = []

        def collect(e: dict) -> None:
            if e.get("kind") == "executed":
                executed.append(e["op"])

        results = await run_task(self.computer, utterance, self.llm,
                                 max_bursts=self.max_bursts,
                                 allow_risky=self.allow_risky,
                                 verbose=self.verbose, on_event=collect)
        status = results[-1].status if results else "aborted"
        # learn ONLY from clean success: a run that hit the burst limit
        # ("observe") or aborted must never become a macro
        learning = bool(executed) and status == "done"
        if learning:
            # learning happens OFF the hot path — the user's task is already
            # done; parameterization is a second LLM call they shouldn't wait on
            import asyncio
            task = asyncio.create_task(self._learn_async(utterance, list(executed)))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        return {
            "tier": "fast",
            "status": status,
            "actions": len(executed),
            "learning": learning,
            "match_ms": round(match_ms),
            "total_ms": round((time.perf_counter() - t0) * 1000),
        }

    async def _learn_async(self, utterance: str, executed: list[dict]) -> None:
        await self._refresh_frontmost()  # the run may have changed the app
        try:
            macro_dict = await parameterize(utterance, debind(executed), self.llm)
        except Exception:  # noqa: BLE001 - learning is best-effort
            return
        if not macro_dict:
            return
        macro_dict.setdefault("guard", infer_guard(macro_dict["actions"]))
        macro = Macro(**{k: macro_dict[k] for k in
                         ("id", "description", "slots", "examples",
                          "actions", "guard")})
        macro.app = self._frontmost  # scope to the app the run landed in
        self.learn(macro)
        if self.verbose:
            print(f"\n  ✓ learned [{macro.id}] slots={macro.slots} "
                  f"(app: {macro.app or 'any'}) — say it again for instant")

    def learn(self, macro: Macro) -> None:
        """Add a macro (e.g. parameterized from a successful LLM-tier run via
        `capture.parameterize`) and re-warm the matcher's prompt prefix."""
        self.store.add(macro)
        if self.tiny.available():
            self.tiny.warm(self.store.templates())
