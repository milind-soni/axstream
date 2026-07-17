"""Voice-driven computer use: speak a task, watch the Mac do it.

Pipeline per utterance:
  push-to-talk record -> local STT (Parakeet/whisper) -> streamed JSONL
  actions executing while the LLM is still generating.

Prereqs: computer-server running (see demo_live.py), CLAUDE_API/ANTHROPIC_API_KEY
in env or .env, and mic permission for your terminal (macOS will prompt once).

Usage:
  uv run python demo_voice.py [--uri ws://localhost:8765/ws] [--stt whisper]
Say "quit" / "exit" to stop.
"""

import argparse
import asyncio
import os
import time

from axstream.computer import Computer
from axstream.llm import stream_anthropic, stream_openai_compat
from axstream.runner import run_task
from axstream.voice import load_transcriber, record_and_transcribe


def load_env_keys() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k == "CLAUDE_API":
                    k = "ANTHROPIC_API_KEY"
                if k and v and k not in os.environ:
                    os.environ[k] = v


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="ws://localhost:8765/ws")
    parser.add_argument("--provider", choices=["groq", "anthropic"], default="groq")
    parser.add_argument("--model", default=None)
    parser.add_argument("--stt", choices=["parakeet", "whisper"], default="parakeet")
    parser.add_argument("--max-bursts", type=int, default=6)
    args = parser.parse_args()

    load_env_keys()
    print("loading local STT model...")
    t0 = time.perf_counter()
    transcriber = load_transcriber(prefer=args.stt)
    # burn the MLX compile warmup now so the first utterance transcribes fast
    import numpy as np

    transcriber.transcribe(np.zeros(8000, dtype="float32"))
    print(f"STT ready: {transcriber.name} in {time.perf_counter() - t0:.1f}s (warmed)")

    computer = Computer(uri=args.uri)
    await computer.connect()

    if args.provider == "groq" and os.environ.get("GROQ_API_KEY"):
        model = args.model or "qwen/qwen3.6-27b"
        extra = {"reasoning_effort": "low" if "gpt-oss" in model else "none"}

        def stream_factory(system: str, user: str):
            return stream_openai_compat(
                system, user, model=model,
                api_key=os.environ["GROQ_API_KEY"],
                base_url="https://api.groq.com/openai/v1", extra=extra,
            )
    else:
        model = args.model or "claude-haiku-4-5-20251001"

        def stream_factory(system: str, user: str):
            return stream_anthropic(system, user, model=model)

    try:
        while True:
            text, timing = await record_and_transcribe(transcriber)
            if not text:
                print("  [voice] heard nothing, try again")
                continue
            print(f'  [voice] "{text}"  (stt {timing["transcribe_ms"]:.0f}ms)')
            if text.lower().rstrip(".!? ") in ("quit", "exit", "stop"):
                break
            t_task = time.perf_counter()
            await run_task(
                computer, text, stream_factory, max_bursts=args.max_bursts
            )
            print(f"  [voice] task loop finished in {time.perf_counter() - t_task:.1f}s\n")
    finally:
        await computer.close()


if __name__ == "__main__":
    asyncio.run(main())
