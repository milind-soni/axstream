"""The zoxide-tier demo: "it learns your commands and makes them instant."

Type a command:
  - First time (novel): the LLM tier (Groq) plans it, executes it streaming,
    and the successful run is captured + parameterized into a macro.
  - Next time (known): the tiny local model (LFM2.5-350M) matches it in ~80ms,
    the cached macro replays with NO LLM, guarded against the live screen.

The HUD prints the tier and the wall-clock so the slow-first / instant-second
contrast is visible. Slots mean variations work: "title it yoyo" replays the
"new note" macro learned from "title it standup".

Prereqs:
  1. computer-server:  cd ../cua/libs/python/computer-server && uv run python -m computer_server --port 8765
  2. tiny model:       llama-server -m ~/models/LFM2.5-350M-Q4_K_M.gguf --port 8791 -ngl 99 -c 4096
  3. GROQ_API_KEY in .env

Usage:  uv run python demo_learn.py
"""

import asyncio
import os
import time

from axstream.capture import infer_guard, parameterize
from axstream.compiler import StreamCompiler
from axstream.computer import Computer
from axstream.executor import Executor
from axstream.llm import stream_openai_compat
from axstream.macros import Macro, MacroStore
from axstream.prompt import SYSTEM, build_user
from axstream.ax import Snapshot
from axstream.tiny import TinyMatcher

RESET, DIM, GREEN, YELLOW, BOLD = "\033[0m", "\033[2m", "\033[32m", "\033[33m", "\033[1m"


def load_env() -> None:
    p = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(p):
        for line in open(p):
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and v and k not in os.environ:
                    os.environ[k] = v


def groq_stream(system: str, user: str):
    return stream_openai_compat(
        system, user, model="qwen/qwen3.6-27b",
        api_key=os.environ["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
        extra={"reasoning_effort": "none"},
    )


async def observe(computer: Computer) -> Snapshot:
    return Snapshot(await computer.ax_tree())


async def run_llm_tier(computer: Computer, task: str) -> tuple[list[dict], bool]:
    """LLM tier: stream one burst, execute it, return the executed actions."""
    snapshot = await observe(computer)
    executor = Executor(computer, snapshot, allow_risky=True)
    user = build_user(task, snapshot.summarize())
    compiler = StreamCompiler()

    executed: list[dict] = []

    async def chunks():
        async for c in groq_stream(SYSTEM, user):
            yield c

    # tap the executor's events to collect what actually ran
    orig = executor.on_event

    def collect(e: dict) -> None:
        if e["kind"] == "executed":
            executed.append(e["op"])
        if orig:
            orig(e)

    executor.on_event = collect
    result = await executor.run_burst(chunks())
    return executed, result.status in ("done", "observe")


async def main() -> None:
    load_env()
    store = MacroStore()
    tiny = TinyMatcher()
    computer = Computer(uri="ws://localhost:8765/ws")
    await computer.connect()

    tiny_ok = tiny.available()
    print(f"{BOLD}axstream — learn-and-replay demo{RESET}")
    print(f"{DIM}tiny matcher: {'ready' if tiny_ok else 'OFFLINE (all commands hit the LLM tier)'} | "
          f"macros loaded: {len(store.macros)}{RESET}")
    if tiny_ok:
        tiny.warm(store.templates())
    print(f"{DIM}type a command (or 'quit'){RESET}\n")

    while True:
        try:
            task = input("» ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not task or task.lower() in ("quit", "exit"):
            break

        t0 = time.perf_counter()

        # --- zoxide tier: tiny match -> replay ---
        hit = tiny.match(task, store.templates()) if tiny_ok else None
        if hit:
            match_ms = (time.perf_counter() - t0) * 1000
            plan = store.resolve(hit["template"], hit.get("slots", {}))
            snapshot = await observe(computer)
            executor = Executor(computer, snapshot, allow_risky=True)
            result = await executor.replay(plan["actions"], plan.get("guard"))
            total = (time.perf_counter() - t0) * 1000
            if result.status != "guard_failed":
                print(f"  {GREEN}⚡ REPLAY{RESET} [{hit['template']}] "
                      f"slots={hit.get('slots', {})}  "
                      f"{DIM}match {match_ms:.0f}ms · total {total:.0f}ms · no LLM{RESET}\n")
                continue
            print(f"  {YELLOW}guard failed — falling back to LLM{RESET}")

        # --- LLM tier: plan, execute, learn ---
        executed, ok = await run_llm_tier(computer, task)
        total = (time.perf_counter() - t0) * 1000
        print(f"  {YELLOW}✦ LLM{RESET}  {len(executed)} actions  {DIM}total {total:.0f}ms{RESET}")

        if ok and executed:
            macro_dict = await parameterize(task, executed, groq_stream)
            if macro_dict:
                macro_dict.setdefault("guard", infer_guard(macro_dict["actions"]))
                store.add(Macro(**{k: macro_dict[k] for k in
                                   ("id", "description", "slots", "examples", "actions", "guard")}))
                tiny.warm(store.templates())
                print(f"  {DIM}↳ learned '{macro_dict['id']}' "
                      f"(slots: {macro_dict['slots']}) — say it again for instant replay{RESET}\n")
            else:
                print(f"  {DIM}↳ could not parameterize; not learned{RESET}\n")
        else:
            print()

    await computer.close()


if __name__ == "__main__":
    asyncio.run(main())
