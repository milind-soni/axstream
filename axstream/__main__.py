"""The axstream CLI.

  axstream replay <name|path>     replay a file macro (--slots, --dry, --learn)
  axstream list [--json]          macros with verified state
  axstream verify <name>          the verify-before-trust gate
  axstream bench <name>           p50/p95 per op across runs
  axstream mcp                    the MCP server (agents)
  axstream install                wire skill + MCP into Claude Code / Codex
  axstream --doctor               check prerequisites, exit 0/1
"""

import os
import sys


def doctor() -> int:
    from .driver import DRIVER_BIN

    ok = os.path.exists(os.path.expanduser(DRIVER_BIN))
    print(f"  [{'ok  ' if ok else 'FAIL'}] cua-driver"
          + ("" if ok else " — install cua-driver (github.com/trycua/cua) "
             "and grant Accessibility"))
    try:
        from . import ocr
        o = ocr.available()
    except Exception:  # noqa: BLE001
        o = False
    print(f"  [{'ok  ' if o else 'opt '}] ocr (text anchors + assertions)")
    from .macrofile import discover
    n = len(discover())
    print(f"  [ok  ] macro library — {n} file macro(s)")
    return 0 if ok else 1


import argparse


def _load_env_file() -> None:
    pass


def main() -> None:
    _load_env_file()
    # file-macro subcommands take their own flags — dispatch before argparse
    argv = sys.argv[1:]
    if argv and argv[0] == "replay":
        from .replay import cmd_replay
        sys.exit(cmd_replay(argv[1:]))
    if argv and argv[0] == "list":
        from .replay import cmd_list
        sys.exit(cmd_list(argv[1:]))
    if argv and argv[0] == "mcp":
        from .mcp import serve
        sys.exit(serve())
    if argv and argv[0] == "install":
        from .install import cmd_install
        sys.exit(cmd_install(argv[1:]))
    if argv and argv[0] == "bench":
        from .replay import cmd_bench
        sys.exit(cmd_bench(argv[1:]))
    if argv and argv[0] == "phone":
        from .phone_cli import cmd_phone
        sys.exit(cmd_phone(argv[1:]))
    if argv and argv[0] == "stats":
        from .ledger import cmd_stats
        sys.exit(cmd_stats(argv[1:]))
    if argv and argv[0] == "verify":
        from .gate import cmd_verify
        sys.exit(cmd_verify(argv[1:]))
    parser = argparse.ArgumentParser(prog="axstream")
    parser.add_argument("--doctor", action="store_true",
                        help="verify prerequisites and exit")
    args = parser.parse_args()
    if args.doctor:
        sys.exit(doctor())
    parser.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
