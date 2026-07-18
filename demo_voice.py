"""Speak a command — the instant tier does it. No cloud, no LLM.

Pipeline per utterance:
  push-to-talk -> local STT (Parakeet on MLX) -> tiny matcher (~100ms) ->
  cua-driver replay (background, pid-addressed)

Uses the same seeded macros as demo_replay.py; slot variations work
("make a text file that says whatever you dictate").

Prereqs:
  - tiny matcher on :8791 (or AXSTREAM_TINY_URL):
      llama-server -m <matcher>.gguf --port 8791 -ngl 99 -c 4096 --no-webui
  - cua-driver installed with Accessibility granted
  - mic permission for your terminal (macOS prompts on first run)
  - voice extras:  uv sync --extra voice

Usage:  uv run python demo_voice.py    (say "quit" to stop)
"""

import asyncio
import time

from axstream.ax import Snapshot
from axstream.driver import DriverComputer
from axstream.executor import Executor
from axstream.macros import MacroStore
from axstream.tiny import TinyMatcher
from axstream.voice import load_transcriber, record_and_transcribe
from demo_replay import SEED

RESET, DIM, GREEN, YELLOW, BOLD = "\033[0m", "\033[2m", "\033[32m", "\033[33m", "\033[1m"


async def main() -> None:
    print("loading local STT...")
    t0 = time.perf_counter()
    transcriber = load_transcriber()
    import numpy as np

    transcriber.transcribe(np.zeros(8000, dtype="float32"))  # burn MLX warmup
    print(f"STT ready: {transcriber.name} ({time.perf_counter() - t0:.1f}s)")

    store = MacroStore(path="~/.axstream/replay_demo.json")
    store.macros.clear()
    for m in SEED:
        store.add(m)

    tiny = TinyMatcher()
    if not tiny.available():
        print("tiny matcher not reachable — start llama-server (see header)")
        return
    tiny.warm(store.templates())

    computer = DriverComputer()
    await computer.connect()

    print(f"{BOLD}axstream — voice → instant tier{RESET}")
    print(f"{DIM}macros: {', '.join(store.macros)} | say 'quit' to stop{RESET}\n")

    try:
        while True:
            text, timing = await record_and_transcribe(transcriber)
            utterance = text.strip().lower().rstrip(".!?")
            if not utterance:
                print(f"  {DIM}(heard nothing){RESET}\n")
                continue
            print(f"  heard: \"{utterance}\"  {DIM}stt {timing['transcribe_ms']:.0f}ms{RESET}")
            if utterance in ("quit", "exit", "stop"):
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
