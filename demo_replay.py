"""The instant (zoxide) tier, standalone — reliable, no cloud, no LLM.

Seeds a couple of verified macros, then: utterance -> tiny local match
(~90ms) -> replay through cua-driver (reliable pid-addressed execution).
Slot variations work: the macro stores {text}/{app}, filled from the utterance.

This is the demo that WORKS today. The learn-from-LLM flywheel (demo_learn.py)
is separate and gated on a reliable planner; this shows the instant tier's
magic on its own.

Prereqs:
  - cua-driver installed (~/.local/bin/cua-driver) with Accessibility granted
  - tiny model:  llama-server -m ~/models/LFM2.5-350M-Q4_K_M.gguf --port 8791 -ngl 99 -c 4096

Usage:  uv run python demo_replay.py
"""

import asyncio
import time

from axstream.ax import Snapshot
from axstream.driver import DriverComputer
from axstream.executor import Executor
from axstream.macros import Macro, MacroStore
from axstream.tiny import TinyMatcher

RESET, DIM, GREEN, YELLOW, BOLD = "\033[0m", "\033[2m", "\033[32m", "\033[33m", "\033[1m"

SEED = [
    Macro(
        id="textfile_saying",
        description="make/open a new text document and type some text into it",
        slots=["text"],
        examples=[
            {"utterance": "make a text file that says hello world", "slots": {"text": "hello world"}},
            {"utterance": "open a new text file saying good morning", "slots": {"text": "good morning"}},
            {"utterance": "new text document with the words see you soon", "slots": {"text": "see you soon"}},
        ],
        actions=[
            {"op": "act", "do": "open", "target": "TextEdit"},
            {"op": "act", "do": "key", "keys": ["cmd", "n"]},
            {"op": "act", "do": "wait", "ms": 500},
            {"op": "act", "do": "type", "text": "{text}"},
        ],
    ),
    Macro(
        id="open_app",
        description="open or launch an application by name",
        slots=["app"],
        examples=[
            {"utterance": "open safari", "slots": {"app": "safari"}},
            {"utterance": "launch spotify", "slots": {"app": "spotify"}},
            {"utterance": "can you open notes for me", "slots": {"app": "notes"}},
        ],
        actions=[{"op": "act", "do": "open", "target": "{app}"}],
    ),
]


async def main() -> None:
    store = MacroStore(path="~/.axstream/replay_demo.json")
    store.macros.clear()
    for m in SEED:
        store.add(m)

    tiny = TinyMatcher()
    if not tiny.available():
        print("tiny model not running on :8791 — start llama-server (see header)")
        return
    tiny.warm(store.templates())

    computer = DriverComputer()
    await computer.connect()

    print(f"{BOLD}axstream — instant tier (tiny match -> cua-driver replay){RESET}")
    print(f"{DIM}macros: {', '.join(store.macros)} | say a variation, or 'quit'{RESET}\n")

    try:
        while True:
            try:
                utterance = input("» ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not utterance or utterance.lower() in ("quit", "exit"):
                break

            t0 = time.perf_counter()
            hit = tiny.match(utterance, store.templates())
            match_ms = (time.perf_counter() - t0) * 1000
            if not hit:
                print(f"  {YELLOW}no macro matched{RESET} "
                      f"{DIM}(match {match_ms:.0f}ms — would fall to the LLM tier){RESET}\n")
                continue

            plan = store.resolve(hit["template"], hit["slots"])
            executor = Executor(computer, Snapshot({}), allow_risky=True)
            result = await executor.replay(plan["actions"], plan.get("guard"))
            total = (time.perf_counter() - t0) * 1000
            print(f"  {GREEN}⚡ REPLAY{RESET} [{hit['template']}] slots={hit['slots']}  "
                  f"{DIM}match {match_ms:.0f}ms · total {total:.0f}ms · no LLM · {result.status}{RESET}\n")
    finally:
        await computer.close()


if __name__ == "__main__":
    asyncio.run(main())
