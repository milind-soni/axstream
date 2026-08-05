"""The axstream CLI.

  axstream replay <name|path>     replay a file macro (see axstream/macrofile.py)
          [--slots '{"k":"v"}']     fill slot placeholders
          [--dry]                   print the resolved actions, execute nothing
  axstream list                   macro files found, with descriptions
  axstream menu                   menu-bar voice launcher (hold ⌃⌥, speak)
  axstream up                     start everything with defaults and listen
  axstream up --voice             same, but listen on the microphone
  axstream "launch safari"        one utterance, JSON result on stdout
  ... | axstream --stdin          one utterance per line, JSON per line
                                  (point your STT app's output here)
  axstream --doctor               check every prerequisite, exit 0/1

(Equivalently: `python -m axstream ...`.)
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

DIM, GREEN, YELLOW, BOLD, RESET = "\033[2m", "\033[32m", "\033[33m", "\033[1m", "\033[0m"

MODEL_CANDIDATES = [
    "~/models/lfm25-350m-axstream-Q4_K_M.gguf",   # fine-tuned matcher (preferred)
    "~/models/LFM2.5-350M-Q4_K_M.gguf",           # stock base (demo grade)
]


def _load_env() -> None:
    """Pick up .env from the CWD or the repo root (dev convenience)."""
    for p in (".env", os.path.join(os.path.dirname(__file__), "..", ".env")):
        if not os.path.exists(p):
            continue
        for line in open(p):
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and v and k not in os.environ:
                    os.environ[k] = v


async def run_utterances(utterances) -> None:
    from .session import Session

    _load_env()
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

    # (name, passed, required, fix) — only REQUIRED checks fail the doctor:
    # `axstream replay`/`list`/`bench` and the agent surfaces need just the
    # driver; the tiny matcher serves only the voice/instant tier and its
    # absence must not read as "axstream is broken" (a coding agent seeing
    # FAIL here wrongly concluded replay was unhealthy).
    checks: list[tuple[str, bool, bool, str]] = []

    matcher_ok = TinyMatcher().available()
    checks.append(("tiny matcher (voice/instant tier only)", matcher_ok, False,
                   f"llama-server not reachable at {DEFAULT_URL} — needed only "
                   "for `axstream up` / voice; start one or run `axstream up`"))

    driver_ok = os.path.exists(os.path.expanduser(DRIVER_BIN))
    checks.append(("cua-driver", driver_ok, True,
                   "install cua-driver (github.com/trycua/cua) and grant "
                   "Accessibility permission"))

    try:
        from . import ocr
        ocr_ok = ocr.available()
    except Exception:  # noqa: BLE001 - a broken pyobjc must not kill doctor
        ocr_ok = False
    checks.append(("ocr (text anchors + assertions)", ocr_ok, False,
                   "pip install axstream — replay works without it "
                   "but loses the OCR rung and text assertions"))

    store = MacroStore()
    ok = True
    for name, passed, required, fix in checks:
        tag = "ok " if passed else ("FAIL" if required else "opt")
        print(f"  [{tag:4}] {name}" + ("" if passed else f" — {fix}"))
        ok = ok and (passed or not required)
    print(f"  [ok  ] macro store — {len(store.macros)} macros at {store.path}")
    return 0 if ok else 1


# -- `axstream up`: start everything with defaults, then listen ---------------

def _find_model() -> str | None:
    env = os.environ.get("AXSTREAM_TINY_MODEL")
    for p in ([env] if env else []) + MODEL_CANDIDATES:
        full = os.path.expanduser(p)
        if os.path.exists(full):
            return full
    return None


def _matcher_model_path(url: str) -> str:
    """Which GGUF a llama-server is actually serving (best-effort)."""
    import httpx

    try:
        r = httpx.get(f"{url}/props", timeout=2)
        return r.json().get("model_path", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _ensure_matcher() -> bool:
    from .tiny import DEFAULT_URL, TinyMatcher

    if TinyMatcher().available():
        served = _matcher_model_path(DEFAULT_URL)
        tuned = _find_model()
        if tuned and "axstream" in tuned and served and "axstream" not in served:
            print(f"  {YELLOW}matcher on {DEFAULT_URL} is serving the BASE model"
                  f" ({os.path.basename(served)}) — the fine-tuned one exists at"
                  f" {tuned}; restart llama-server with it for ~93% vs ~47%{RESET}")
        else:
            print(f"  {GREEN}matcher{RESET} {DIM}up at {DEFAULT_URL}"
                  f" ({os.path.basename(served) or 'model unknown'}){RESET}")
        return True

    model = _find_model()
    if model is None:
        print(f"  {YELLOW}no matcher model found{RESET} — download one "
              "(docs/quickstart) or set AXSTREAM_TINY_MODEL")
        return False
    if shutil.which("llama-server") is None:
        print(f"  {YELLOW}llama-server not installed{RESET} — brew install llama.cpp")
        return False

    port = DEFAULT_URL.rsplit(":", 1)[-1].rstrip("/") if ":" in DEFAULT_URL else "8791"
    print(f"  starting llama-server on :{port} with {os.path.basename(model)} ...")
    subprocess.Popen(
        ["llama-server", "-m", model, "--port", port, "-ngl", "99",
         "-c", "4096", "--no-webui"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # outlives this CLI; `up` reuses it next time
    )
    tiny = TinyMatcher()
    for _ in range(60):
        if tiny.available():
            print(f"  {GREEN}matcher{RESET} {DIM}ready{RESET}")
            return True
        time.sleep(0.5)
    print(f"  {YELLOW}matcher did not come up within 30s{RESET}")
    return False


def _ensure_driver() -> bool:
    from .driver import DRIVER_BIN

    binary = os.path.expanduser(DRIVER_BIN)
    if not os.path.exists(binary):
        print(f"  {YELLOW}cua-driver not installed{RESET} — "
              '/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"')
        return False

    def alive() -> bool:
        try:
            r = subprocess.run([binary, "call", "get_screen_size", "{}"],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    if alive():
        print(f"  {GREEN}driver{RESET} {DIM}daemon responding{RESET}")
        return True
    subprocess.run(["open", "-a", "CuaDriver"], capture_output=True)
    for _ in range(20):
        if alive():
            print(f"  {GREEN}driver{RESET} {DIM}daemon started{RESET}")
            return True
        time.sleep(0.5)
    print(f"  {YELLOW}driver daemon not responding{RESET} — open CuaDriver.app and "
          "check Accessibility permission")
    return False


def _launch_menu_bar() -> int:
    """Start the native menu-bar app (swift/AxstreamBar): hold ⌃⌥ and speak
    a workflow — whisper.cpp + the tiny matcher + macro replay."""
    from pathlib import Path
    if subprocess.run(["pgrep", "-x", "AxstreamBar"],
                      capture_output=True).returncode == 0:
        print("Axstream is already in the menu bar (hold ⌃⌥ and speak)")
        return 0
    repo = Path(__file__).resolve().parent.parent
    candidates = [
        Path("~/.axstream/bin/AxstreamBar").expanduser(),
        repo / "swift/AxstreamBar/.build/release/AxstreamBar",
    ]
    binary = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if binary is None:
        print("AxstreamBar is not built. Build it with:\n"
              f"  cd {repo / 'swift/AxstreamBar'} && swift build -c release",
              file=sys.stderr)
        return 1
    log = Path("~/.axstream/bar.log").expanduser()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launching AxstreamBar\n")
        subprocess.Popen([str(binary)], stdout=handle, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    print("Axstream is in the menu bar — hold ⌃⌥ and speak a workflow")
    return 0


def _print_result(res: dict) -> None:
    tier = res.get("tier")
    if tier == "instant":
        status = "done" if res["status"] == "done" else res["status"]
        print(f"{status}  {res['total_ms'] / 1000:.1f}s  no llm\n")
    elif tier == "fast":
        tail = "  learning" if res.get("learning") else ""
        print(f"{res['status']}  {res['total_ms'] / 1000:.1f}s{tail}\n")
    else:
        print(f"no macro  ({res['match_ms']}ms)\n")


async def _up(voice: bool) -> None:
    from .session import Session

    _load_env()
    print(f"{BOLD}axstream up{RESET}")
    matcher_ok = _ensure_matcher()
    driver_ok = _ensure_driver()
    if not (matcher_ok and driver_ok):
        print("\nfix the above, or run `axstream --doctor` for details")
        sys.exit(1)

    from .macros import MacroStore
    if not MacroStore().macros:  # first run: seed the starter library
        from .packs import load_packs
        from .starter import STARTER
        store = MacroStore()
        pack_macros, _ = load_packs()
        for m in STARTER + pack_macros:
            store.add(m)
        print(f"  {GREEN}seeded{RESET} {DIM}{len(store.macros)} starter macros "
              f"(first run) -> {store.path}{RESET}")

    session = await Session(verbose=True).connect()
    ready = session.ready()
    fast = "on" if session.llm else "OFF (no OPENROUTER_API_KEY / GROQ_API_KEY)"
    print(f"  {GREEN}session{RESET} {DIM}{ready['macros']} macros · "
          f"fast tier {fast}{RESET}\n")

    try:
        if voice:
            await _voice_loop(session)
        else:
            print(f"{DIM}type a command ('quit' to stop, ctrl-c cancels a running one){RESET}")
            while True:
                try:
                    u = (await asyncio.to_thread(input, "» ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not u or u.lower() in ("quit", "exit"):
                    break
                try:
                    _print_result(await session.handle(u))
                except KeyboardInterrupt:
                    print(f"\n  {YELLOW}cancelled{RESET} — still listening\n")
    finally:
        await session.close()


async def _voice_loop(session) -> None:
    try:
        from .voice import listen_and_transcribe, load_transcriber
    except Exception:  # noqa: BLE001
        print(f"{YELLOW}voice extras missing{RESET} — uv sync --extra voice")
        sys.exit(1)
    import numpy as np

    print(f"{DIM}loading local STT ...{RESET}")
    transcriber = load_transcriber()
    transcriber.transcribe(np.zeros(8000, dtype="float32"))
    print(f"{DIM}listening — just speak, pause to commit "
          f"('quit' to stop, ctrl-c cancels a running command){RESET}")
    while True:
        try:
            text, timing = await listen_and_transcribe(transcriber)
        except KeyboardInterrupt:
            break
        u = text.strip().lower().rstrip(".!?")
        if not u:
            continue
        print(f"» {u}")
        if u in ("quit", "exit", "stop"):
            break
        try:
            _print_result(await session.handle(u))
        except KeyboardInterrupt:
            print(f"\n  {YELLOW}cancelled{RESET} — still listening\n")


def _load_env_file() -> None:
    """Merge ~/.axstream/env (KEY=VALUE lines) into the environment so API
    keys reach every entry point — CLI, MCP server, and the menu-bar app's
    spawned engine calls — without depending on shell profiles."""
    from pathlib import Path
    path = Path("~/.axstream/env").expanduser()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


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
    if argv and argv[0] == "verify":
        from .gate import cmd_verify
        sys.exit(cmd_verify(argv[1:]))
    if argv and argv[0] == "menu":
        sys.exit(_launch_menu_bar())

    parser = argparse.ArgumentParser(prog="axstream")
    parser.add_argument("utterance", nargs="?",
                        help="one utterance to handle (or a subcommand: "
                             "up / seed / replay / list / menu)")
    parser.add_argument("--voice", action="store_true",
                        help="with `up`: listen on the microphone")
    parser.add_argument("--stdin", action="store_true",
                        help="read utterances line by line from stdin")
    parser.add_argument("--doctor", action="store_true",
                        help="verify prerequisites and exit")
    args = parser.parse_args()

    if args.doctor:
        sys.exit(doctor())
    if args.utterance == "seed":
        from .macros import MacroStore
        from .packs import load_packs
        from .starter import STARTER

        store = MacroStore()
        before = len(store.macros)
        pack_macros, rejects = load_packs(verbose=True)
        for m in STARTER + pack_macros:
            store.add(m)
        print(f"seeded {len(store.macros) - before} macros "
              f"({len(STARTER)} starter + {len(pack_macros)} from packs, "
              f"{len(rejects)} rejected) -> {len(store.macros)} total at {store.path}")
        return
    if args.utterance == "up":
        asyncio.run(_up(voice=args.voice))
    elif args.stdin:
        asyncio.run(run_utterances(sys.stdin))
    elif args.utterance:
        asyncio.run(run_utterances([args.utterance]))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
