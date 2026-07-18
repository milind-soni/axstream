"""CLI for integrators — no Python required on their side.

  python -m axstream "launch safari"     one utterance, JSON result on stdout
  ... | python -m axstream --stdin       one utterance per line, JSON per line
                                         (point your STT app's output here)
  python -m axstream --doctor            check every prerequisite, exit 0/1
"""

import argparse
import asyncio
import json
import os
import sys


async def run_utterances(utterances) -> None:
    from .session import Session

    session = await Session().connect()
    try:
        for u in utterances:
            u = u.strip()
            if not u:
                continue
            print(json.dumps(await session.handle(u)), flush=True)
    finally:
        await session.close()


def doctor() -> int:
    from .driver import DRIVER_BIN
    from .macros import MacroStore
    from .tiny import DEFAULT_URL, TinyMatcher

    checks: list[tuple[str, bool, str]] = []

    matcher_ok = TinyMatcher().available()
    checks.append(("tiny matcher", matcher_ok,
                   f"llama-server not reachable at {DEFAULT_URL} — start one "
                   "(see docs/quickstart) or set AXSTREAM_TINY_URL"))

    driver_ok = os.path.exists(os.path.expanduser(DRIVER_BIN))
    checks.append(("cua-driver", driver_ok,
                   "install cua-driver (github.com/trycua/cua) and grant "
                   "Accessibility permission"))

    store = MacroStore()
    ok = True
    for name, passed, fix in checks:
        print(f"  [{'ok ' if passed else 'FAIL'}] {name}"
              + ("" if passed else f" — {fix}"))
        ok = ok and passed
    print(f"  [ok ] macro store — {len(store.macros)} macros at {store.path}")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="axstream")
    parser.add_argument("utterance", nargs="?", help="one utterance to handle")
    parser.add_argument("--stdin", action="store_true",
                        help="read utterances line by line from stdin")
    parser.add_argument("--doctor", action="store_true",
                        help="verify prerequisites and exit")
    args = parser.parse_args()

    if args.doctor:
        sys.exit(doctor())
    if args.stdin:
        asyncio.run(run_utterances(sys.stdin))
    elif args.utterance:
        asyncio.run(run_utterances([args.utterance]))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
