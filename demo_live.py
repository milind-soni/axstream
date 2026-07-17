"""Milestone B live run: real LLM stream driving the real computer-server.

Prereqs:
  1. Start cua's computer-server locally (grants: Accessibility + Screen Recording):
       cd ../cua/libs/python/computer-server && uv run python -m computer_server
  2. export ANTHROPIC_API_KEY=...   (or OPENAI_API_KEY with --provider openai)

Usage:
  uv run python demo_live.py --task "open TextEdit and type hello world"
"""

import argparse
import asyncio
import os

from axstream.computer import Computer
from axstream.llm import stream_anthropic, stream_openai_compat
from axstream.runner import run_task
from demo_voice import load_env_keys


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--uri", default="ws://localhost:8000/ws")
    parser.add_argument("--provider", choices=["anthropic", "openai", "groq"], default="groq")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument("--max-bursts", type=int, default=8)
    parser.add_argument("--no-risky", action="store_true",
                        help="block actions marked risk=risky instead of executing them")
    args = parser.parse_args()
    load_env_keys()

    if args.provider == "anthropic":
        model = args.model or "claude-haiku-4-5-20251001"

        def stream_factory(system: str, user: str):
            return stream_anthropic(system, user, model=model)
    elif args.provider == "groq":
        model = args.model or "qwen/qwen3.6-27b"
        extra = {"reasoning_effort": "low"} if "gpt-oss" in model else None

        def stream_factory(system: str, user: str):
            return stream_openai_compat(
                system, user, model=model,
                api_key=os.environ.get("GROQ_API_KEY"),
                base_url="https://api.groq.com/openai/v1", extra=extra,
            )
    else:
        model = args.model or "gpt-4.1-mini"

        def stream_factory(system: str, user: str):
            return stream_openai_compat(system, user, model=model, base_url=args.base_url)

    computer = Computer(uri=args.uri)
    try:
        await computer.connect()
    except Exception as e:
        raise SystemExit(
            f"could not connect to computer-server at {args.uri}: {e}\n"
            "start it with: cd ../cua/libs/python/computer-server && "
            "uv run python -m computer_server"
        )

    try:
        await run_task(
            computer,
            args.task,
            stream_factory,
            max_bursts=args.max_bursts,
            allow_risky=not args.no_risky,
        )
    finally:
        await computer.close()


if __name__ == "__main__":
    asyncio.run(main())
