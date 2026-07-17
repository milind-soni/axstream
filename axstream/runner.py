"""Burst loop: observe -> stream -> execute (pipelined) -> repeat.

Also owns the timeline printout that shows decode/execution overlap.
"""

from __future__ import annotations

import time
from typing import AsyncIterator, Callable

from .ax import Snapshot
from .computer import Computer
from .executor import BurstResult, Executor
from .prompt import SYSTEM, build_user

StreamFactory = Callable[[str, str], AsyncIterator[str]]
# (system, user) -> async iterator of text chunks


async def run_task(
    computer: Computer,
    task: str,
    stream_factory: StreamFactory,
    max_bursts: int = 8,
    allow_risky: bool = True,
    verbose: bool = True,
) -> list[BurstResult]:
    results: list[BurstResult] = []
    history_lines: list[str] = []

    for burst_index in range(max_bursts):
        t_obs = time.perf_counter()
        state = await computer.ax_tree()
        snapshot = Snapshot(state)
        observation = snapshot.summarize()
        obs_ms = (time.perf_counter() - t_obs) * 1000
        if verbose:
            print(
                f"\n== burst {burst_index}: observed {len(snapshot.elements)} elements "
                f"in {obs_ms:.0f}ms ({len(observation)} chars) =="
            )

        user = build_user(task, observation, "\n".join(history_lines[-20:]))
        executor = Executor(
            computer,
            snapshot,
            allow_risky=allow_risky,
            on_event=_printer() if verbose else None,
        )
        result = await executor.run_burst(stream_factory(SYSTEM, user))
        results.append(result)

        for e in result.events:
            if e["kind"] == "executed":
                history_lines.append(f"did {_op_str(e['op'])}")
            elif e["kind"] == "action_failed":
                history_lines.append(f"FAILED {_op_str(e['op'])}: {e['error']}")

        if verbose:
            _print_summary(result)
        if result.status in ("done", "aborted"):
            break
    return results


def _op_str(op: dict) -> str:
    do = op.get("do", op.get("op"))
    target = op.get("target")
    if isinstance(target, dict) and "ax" in target:
        target = op.get("resolved") or target["ax"].get("title") or target["ax"].get("id")
    detail = target or op.get("text") or op.get("keys") or ""
    return f"{do} {detail}".strip()


def _printer() -> Callable[[dict], None]:
    def on_event(e: dict) -> None:
        t = e["t"]
        kind = e["kind"]
        if kind == "executed":
            exec_ms = (e["t_done"] - e["t_start"]) * 1000
            print(f"  t={t:6.2f}s  > {_op_str(e['op'])}  ({exec_ms:.0f}ms)")
        elif kind == "narration":
            print(f"  t={t:6.2f}s  . {e['text']}")
        elif kind == "invalid_line":
            print(f"  t={t:6.2f}s  ! invalid line: {e['error']}  {e['line'][:80]}")
        elif kind == "action_failed":
            print(f"  t={t:6.2f}s  X {_op_str(e['op'])} failed: {e['error']}")
        else:
            print(f"  t={t:6.2f}s  - {kind}")

    return on_event


def _print_summary(result: BurstResult) -> None:
    executed = [e for e in result.events if e["kind"] == "executed"]
    if not executed:
        print(f"  [{result.status}] no actions executed")
        return
    first_action = executed[0]["t_start"]
    stream_end = result.stream_ended_at or 0.0
    print(
        f"  [{result.status}] {len(executed)} actions | first action at "
        f"{first_action:.2f}s | stream ended {stream_end:.2f}s | "
        f"{result.overlap_seconds():.2f}s of execution overlapped generation"
    )
