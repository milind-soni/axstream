"""Pipelined executor: actions run while the LLM is still generating.

Producer (LLM stream -> StreamCompiler) and consumer (this executor) are
connected by an asyncio.Queue, so execution overlaps decode. Every event is
timestamped for the overlap timeline.

Late binding: ax targets are resolved against the burst's Snapshot at
execution time; if resolution fails, the tree is re-fetched once and retried
before the burst aborts. Risk gating: "risky" ops are executed only if the
policy allows (default: allow, but every risky op is logged).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from .ax import Snapshot
from .compiler import StreamCompiler
from .computer import Computer
from .spec import risk_of


@dataclass
class BurstResult:
    status: str  # "done" | "observe" | "aborted" | "stream_end"
    reason: str = ""
    events: list[dict] = field(default_factory=list)
    stream_ended_at: Optional[float] = None
    last_action_done_at: Optional[float] = None

    def overlap_seconds(self) -> float:
        """How much execution ran in parallel with generation."""
        if self.stream_ended_at is None or self.last_action_done_at is None:
            return 0.0
        acted_during_stream = [
            e for e in self.events
            if e["kind"] == "executed" and e["t_done"] <= self.stream_ended_at
        ]
        if not acted_during_stream:
            return 0.0
        return acted_during_stream[-1]["t_done"] - acted_during_stream[0]["t_start"]


class Executor:
    def __init__(
        self,
        computer: Computer,
        snapshot: Snapshot,
        allow_risky: bool = True,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.computer = computer
        self.snapshot = snapshot
        self.allow_risky = allow_risky
        self.on_event = on_event
        self._t0 = time.perf_counter()

    def _now(self) -> float:
        return time.perf_counter() - self._t0

    def _emit(self, result: BurstResult, kind: str, **data) -> None:
        event = {"kind": kind, "t": self._now(), **data}
        result.events.append(event)
        if self.on_event:
            self.on_event(event)

    async def run_burst(self, chunks: AsyncIterator[str]) -> BurstResult:
        """Consume an LLM text stream, executing actions as lines complete."""
        result = BurstResult(status="stream_end")
        queue: asyncio.Queue = asyncio.Queue()
        compiler = StreamCompiler(fenced=True)

        async def produce() -> None:
            try:
                async for chunk in chunks:
                    for event in compiler.push(chunk):
                        await queue.put(event)
                for event in compiler.finish():
                    await queue.put(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - surface stream errors to the burst
                await queue.put(("stream_error", str(e)))
            finally:
                result.stream_ended_at = self._now()
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                stop = await self._handle(event, result)
                if stop:
                    producer.cancel()
                    break
        finally:
            if not producer.done():
                producer.cancel()
        return result

    async def _handle(self, event: tuple, result: BurstResult) -> bool:
        """Execute one compiler event. Returns True when the burst should stop."""
        kind = event[0]
        if kind == "text":
            self._emit(result, "narration", text=event[1])
            return False
        if kind == "invalid":
            self._emit(result, "invalid_line", line=event[1], error=event[2])
            return False
        if kind == "stream_error":
            self._emit(result, "stream_error", error=event[1])
            result.status = "aborted"
            result.reason = f"llm stream failed: {event[1][:200]}"
            return True

        op = event[1]
        if op["op"] == "observe":
            result.status = "observe"
            self._emit(result, "observe_requested")
            return True
        if op["op"] == "done":
            result.status = "done"
            result.reason = op.get("reason", "")
            self._emit(result, "done", status=op.get("status"), reason=result.reason)
            return True
        if op["op"] == "assert":
            el = self.snapshot.resolve_element(op["target"].get("ax", {}))
            if el is None:
                el = await self._refresh_and_resolve(op["target"].get("ax", {}))
            self._emit(result, "assert", target=op["target"], ok=el is not None)
            if el is None:
                result.status = "aborted"
                result.reason = f"assert failed: {op['target']}"
                return True
            return False

        # op == "act"
        if risk_of(op) == "risky" and not self.allow_risky:
            self._emit(result, "risky_blocked", op=op)
            result.status = "aborted"
            result.reason = "risky action blocked by policy"
            return True

        # resolve ax targets up front so telemetry/history record what was hit
        resolved = None
        target = op.get("target")
        if isinstance(target, dict) and "ax" in target:
            resolved = self.snapshot.resolve_element(target["ax"])
            if resolved is not None:
                op = {**op, "resolved": f"{resolved.role} {resolved.title!r}"}

        t_start = self._now()
        try:
            await self._execute(op)
        except Exception as e:  # noqa: BLE001 - burst aborts on any executor error
            self._emit(result, "action_failed", op=op, error=str(e), t_start=t_start)
            result.status = "aborted"
            result.reason = f"{op.get('do')}: {e}"
            return True
        t_done = self._now()
        result.last_action_done_at = t_done
        self._emit(result, "executed", op=op, t_start=t_start, t_done=t_done)
        return False

    async def replay(self, actions: list[dict], guard: Optional[dict] = None) -> BurstResult:
        """Zoxide-tier replay: run a cached action stream with no LLM. The
        guard (an ax target) must resolve against the live screen first; a
        guard miss returns status 'guard_failed' so the caller can fall back
        to the LLM tier. Reuses the same per-op execution + late binding as a
        streamed burst, so replayed actions behave identically."""
        result = BurstResult(status="done")
        if guard is not None:
            ax = guard.get("ax", {})
            el = self.snapshot.resolve_element(ax) or await self._refresh_and_resolve(ax)
            if el is None:
                result.status = "guard_failed"
                result.reason = f"guard did not resolve: {guard}"
                self._emit(result, "guard_failed", guard=guard)
                return result
        for op in actions:
            if op.get("op") != "act":
                continue
            resolved = None
            target = op.get("target")
            if isinstance(target, dict) and "ax" in target:
                resolved = self.snapshot.resolve_element(target["ax"])
                if resolved is not None:
                    op = {**op, "resolved": f"{resolved.role} {resolved.title!r}"}
            t_start = self._now()
            try:
                await self._execute(op)
            except Exception as e:  # noqa: BLE001 - abort replay, caller may fall back
                self._emit(result, "action_failed", op=op, error=str(e), t_start=t_start)
                result.status = "aborted"
                result.reason = f"{op.get('do')}: {e}"
                return result
            result.last_action_done_at = self._now()
            self._emit(result, "executed", op=op, t_start=t_start, t_done=result.last_action_done_at)
        return result

    async def _execute(self, op: dict) -> None:
        do = op["do"]
        if do == "wait":
            await asyncio.sleep(op["ms"] / 1000)
            return
        if do == "type":
            await self.computer.type_text(op["text"])
            return
        if do == "key":
            keys = op["keys"]
            await self.computer.key([keys] if isinstance(keys, str) else keys)
            return
        if do == "scroll":
            await self.computer.scroll(op["direction"], op.get("clicks", 1))
            return
        if do == "open":
            await self.computer.open(op["target"])
            return
        # coordinate-taking actions: click / double_click / move
        x, y = await self._coords(op["target"], do)
        if do == "click":
            await self.computer.click(x, y)
        elif do == "double_click":
            await self.computer.double_click(x, y)
        elif do == "move":
            await self.computer.move(x, y)
        else:
            raise ValueError(f"unhandled action: {do}")

    async def _coords(self, target: dict, do: str) -> tuple[float, float]:
        if "x" in target:
            return target["x"], target["y"]
        ax = target["ax"]
        center = self.snapshot.resolve(ax)
        if center is None:
            el = await self._refresh_and_resolve(ax)
            center = el.center if el else None
        if center is None:
            raise LookupError(f"could not resolve target {ax}")
        return center

    async def _refresh_and_resolve(self, ax: dict):
        """Late-binding fallback: re-fetch the live tree once and retry.
        Backends without observation (or a failed fetch) resolve to None so a
        guard miss degrades to guard_failed/LLM-fallback, never a crash."""
        try:
            state = await self.computer.ax_tree()
        except Exception:  # noqa: BLE001 - includes AttributeError on ax_tree-less backends
            return None
        self.snapshot = Snapshot(state)
        # ids are only stable within the burst's original snapshot
        if ax.get("id") and not (ax.get("role") or ax.get("title")):
            return None
        return self.snapshot.resolve_element({k: v for k, v in ax.items() if k != "id"})
