"""`axstream mcp` — fast computer-use primitives + macros as MCP tools.

A minimal Model Context Protocol server over stdio: newline-delimited
JSON-RPC 2.0, hand-rolled instead of pulling in an SDK (the repo's
no-heavy-deps doctrine; the protocol surface we need — initialize,
tools/list, tools/call — is tiny and stable).

Two tool families. The ACCELERATOR primitives complement a host agent's
native computer use — they collapse the expensive legs of its
perceive→reason→act loop into cheap on-device calls (text instead of
screenshot tokens, one batched call instead of a model turn per action):

  screen_text            the window as OCR'd TEXT with coordinates, ~200ms —
                         replaces a screenshot + vision reasoning
  find                   locate a control/text on screen, ~30-600ms —
                         returns a click-ready target
  check                  "is <text> visible?" outcome poll, ~250ms —
                         replaces a verification screenshot
  act                    execute a BATCH of verified actions in one call —
                         replaces a model turn per step

And the macro flywheel, mirroring the CLI contract the skill teaches:

  list_macros            what's replayable (name/description/when_to_use/slots)
  replay_macro           run one (dry or live); returns the JSONL progress
                         lines verbatim — failure handoff and
                         unverified_steps included, same as the CLI
  read_macro             the macro file text, for inspection/refinement
  write_macro            validate + save a macro file (the flywheel: an agent
                         that just did a task manually saves it here)

Register (Codex ~/.codex/config.toml):

  [mcp_servers.axstream]
  command = "axstream"
  args = ["mcp"]

or Claude Code: `claude mcp add axstream -- axstream mcp`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .macrofile import (
    MacroFile,
    MacroFileError,
    discover,
    load,
    macro_dirs,
    parse,
    resolve_name,
    save,
)

PROTOCOL_VERSION = "2025-06-18"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("axstream")
    except Exception:  # noqa: BLE001 - dev checkouts without dist metadata
        return "dev"

_APP_ARG = {"type": "string",
            "description": "App name (e.g. \"Safari\") — targets that app's "
                           "front window. Omit for the frontmost app."}

TOOLS = [
    {
        "name": "screen_text",
        "description": (
            "Read the target window as TEXT: every rendered text line with "
            "its position, via on-device OCR in ~200ms. Use this INSTEAD of "
            "taking a screenshot when you need to know what's on screen — "
            "it costs ~2KB of text rather than a vision-model pass. "
            "Coordinates are window-screenshot pixels; to click something "
            "you found, prefer act with a {\"text\": ...} target (it "
            "re-verifies the text at click time)."),
        "inputSchema": {"type": "object", "properties": {"app": _APP_ARG},
                        "additionalProperties": False},
    },
    {
        "name": "find",
        "description": (
            "Locate rendered text/a labeled control in the target window "
            "(~30ms fast pass, ~600ms retry). Returns whether it's visible, "
            "where, and a ready-to-use click target for act. Use instead of "
            "scanning a screenshot for one thing."),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "app": _APP_ARG},
            "required": ["text"], "additionalProperties": False,
        },
    },
    {
        "name": "check",
        "description": (
            "Outcome verification: polls up to ~2.5s for <text> to be "
            "visible in the target window (~250ms when it's already there). "
            "Use after an action instead of a verification screenshot — "
            "e.g. check the results page shows your query."),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "app": _APP_ARG},
            "required": ["text"], "additionalProperties": False,
        },
    },
    {
        "name": "act",
        "description": (
            "Execute a BATCH of UI actions in ONE call — plan the whole "
            "sequence once instead of one model turn per step. ops is a "
            "list of axstream spec ops, e.g.:\n"
            '  [{"op":"act","do":"open","target":"Safari"},\n'
            '   {"op":"act","do":"key","keys":["cmd","t"]},\n'
            '   {"op":"act","do":"type","text":"hello"},\n'
            '   {"op":"act","do":"key","keys":["enter"]},\n'
            '   {"op":"assert","target":{"text":"hello"}}]\n'
            "Clicks resolve through a verified ladder (AX element -> OCR "
            "text -> pixels): target {\"text\": \"Save\"} or {\"ax\": "
            "{\"title\": \"Save\"}} beats coordinates. Put assert ops after "
            "risky steps — the batch stops at the first failure and returns "
            '{"failed_at", "reason", "completed"} so you can take over at '
            "exactly that step. Actions run on the user's real machine: "
            "keep batches short and assert-guarded. If the sequence is "
            "worth repeating, save it with write_macro afterwards."),
        "inputSchema": {
            "type": "object",
            "properties": {"ops": {"type": "array", "items": {"type": "object"},
                                   "description": "spec ops, executed in order"}},
            "required": ["ops"], "additionalProperties": False,
        },
    },
    {
        "name": "list_macros",
        "description": (
            "List every replayable axstream macro (recorded/authored macOS UI "
            "tasks). Call this BEFORE driving any macOS app's UI manually — "
            "if a macro matches the task, replay_macro finishes it in seconds "
            "with no per-step reasoning."),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    {
        "name": "replay_macro",
        "description": (
            "Replay a macro through the cua-driver daemon (background, "
            "pid-addressed — never steals the user's mouse). Returns one JSON "
            "progress object per action plus a summary. dry=true resolves and "
            "prints the plan WITHOUT executing (always dry-run an unfamiliar "
            "macro first). On failure the last object is "
            '{"failed_at", "op", "reason", "completed"} — take the task over '
            "at exactly that op. A summary with unverified_steps means some "
            "clicks were blind: confirm the end state before trusting the "
            "run."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "macro name or file path"},
                "slots": {"type": "object",
                          "description": "slot values, e.g. {\"query\": \"weather\"}"},
                "dry": {"type": "boolean",
                        "description": "resolve without executing (default false)"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_macro",
        "description": (
            "The verify-before-trust gate: replays the macro live ONCE and "
            "requires its TERMINAL ASSERT to pass, then stamps it verified "
            "(editing the macro invalidates the stamp). Run after "
            "write_macro — but ONLY when re-executing the task is safe: the "
            "gate refuses macros with risk:\"risky\" ops, and you must not "
            "verify anything irreversible (sending, paying, deleting). An "
            "unverified macro may replay to 100% and still not do the task."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "slots": {"type": "object"}},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "read_macro",
        "description": ("Read a macro file's raw text (header line + one op "
                        "per line) for inspection or refinement."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_macro",
        "description": (
            "Validate and save a macro file to ~/.axstream/macros/<name>"
            ".axstream — the flywheel: after finishing a macOS UI task "
            "manually, save it so next time it replays in seconds. Content "
            "is the full file text: line 1 a JSON header (name, description, "
            "when_to_use, slots, optionally \"window\": the window bounds "
            "{x,y,w,h} at authoring time — makes coordinate clicks survive "
            "resizes), then one op per line. Prefer key shortcuts over "
            "clicking chrome; never target text the macro itself types. "
            "Rejected (with the reason) if any line fails spec validation."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "file stem, kebab-case"},
                "content": {"type": "string",
                            "description": "the full .axstream file text"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
    },
]


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_list_macros() -> dict:
    from .gate import verification

    rows = []
    for path, mf in discover():
        if isinstance(mf, MacroFileError):
            rows.append({"file": str(path), "error": str(mf)})
            continue
        rows.append({"name": mf.name, "description": mf.description,
                     "when_to_use": mf.when_to_use,
                     "slots": sorted(mf.slots), "actions": len(mf.actions),
                     "verified": verification(mf)["state"],
                     "file": str(path)})
    if not rows:
        return _text_result("no macros found (searched "
                            + ", ".join(str(d) for d in macro_dirs()) + ")")
    return _text_result("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))


def _tool_replay(args: dict) -> dict:
    name = args.get("name") or ""
    slots = args.get("slots") or {}
    dry = bool(args.get("dry"))
    path = resolve_name(name)
    if path is None:
        return _text_result(f"no macro named {name!r} (searched "
                            + ", ".join(str(d) for d in macro_dirs()) + ")",
                            is_error=True)
    try:
        mf = load(path)
        if dry:
            for slot_name, spec in (mf.slots or {}).items():
                if slot_name not in slots:
                    example = spec.get("example") if isinstance(spec, dict) else None
                    slots[slot_name] = (str(example) if example not in (None, "")
                                        else f"<{slot_name}>")
        actions = mf.fill(slots)
        recorded_window = mf.extra.get("window")
        if isinstance(recorded_window, dict):
            from .geometry import annotate_window_relative

            actions = annotate_window_relative(actions, recorded_window)
    except MacroFileError as e:
        return _text_result(f"{e} (file: {path})", is_error=True)

    lines: list[dict] = []
    if dry:
        lines = [{"i": i, "op": op, "dry": True} for i, op in enumerate(actions)]
        lines.append({"dry": True, "ok": True, "macro": mf.name,
                      "actions": len(actions)})
        code = 0
    else:
        from .driver import DriverComputer
        from .replay import run_actions

        async def go() -> int:
            computer = DriverComputer()
            if mf.extra.get("delivery") == "foreground": computer.delivery = "foreground"
            await computer.connect()
            await computer.fast_cursor()
            try:
                return await run_actions(actions, computer, emit=lines.append)
            finally:
                await computer.close()

        code = asyncio.run(go())
    return _text_result("\n".join(json.dumps(l, ensure_ascii=False) for l in lines),
                        is_error=code != 0)


def _tool_read(args: dict) -> dict:
    path = resolve_name(args.get("name") or "")
    if path is None:
        return _text_result(f"no macro named {args.get('name')!r}", is_error=True)
    return _text_result(path.read_text())


def _tool_write(args: dict) -> dict:
    from .gate import archive, terminal_assert, upsert_conflicts

    name = (args.get("name") or "").strip()
    content = args.get("content") or ""
    if not name or "/" in name or name.startswith("."):
        return _text_result(f"bad macro name {name!r} — use a plain kebab-case "
                            "file stem", is_error=True)
    try:
        mf = parse(content, name_hint=name)
    except MacroFileError as e:
        return _text_result(f"invalid macro: {e}", is_error=True)
    dest = Path("~/.axstream/macros").expanduser() / f"{name}.axstream"
    existed = dest.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # upsert-by-signature (PreAct compile-extend-replace): a macro doing the
    # same task under another name is superseded into .archive/, not left to
    # rot beside its twin
    replaced = [str(archive(p)) for p in upsert_conflicts(mf, exclude_stem=name)]
    dest.write_text(content if content.endswith("\n") else content + "\n")
    verb = "updated" if existed else "saved"
    msg = f"{verb} {mf.name!r} ({len(mf.actions)} actions) -> {dest}"
    if replaced:
        msg += "\nsuperseded same-task macro(s), archived: " + ", ".join(replaced)
    if terminal_assert(mf):
        msg += ("\nUNVERIFIED until gated: run verify_macro (one live replay; "
                "its terminal assert must pass) when a live run is safe.")
    else:
        msg += ("\nNo terminal assert — this macro cannot prove its outcome "
                "and can never be marked verified. Add a final "
                '{"op":"assert","target":{"text":...}} that only passes when '
                "the task worked.")
    return _text_result(msg)


def _tool_verify(args: dict) -> dict:
    from .gate import verify

    name = (args.get("name") or "").strip()
    if not name:
        return _text_result("name is required", is_error=True)
    lines: list[str] = []
    result = verify(name, args.get("slots") or {},
                    emit=lambda line: lines.append(json.dumps(line, ensure_ascii=False)))
    lines.append(json.dumps(result, ensure_ascii=False))
    return _text_result("\n".join(lines), is_error=not result.get("ok"))


async def _targeted_computer(app: str | None):
    """A connected DriverComputer, pinned to `app`'s window when given."""
    from .driver import DriverComputer

    c = DriverComputer()
    await c.connect()
    if app:
        wins = (await c.tool("list_windows")).get("windows", [])
        mine = [w for w in wins
                if (w.get("app_name") or "").lower() == app.lower() and w.get("title")]
        if not mine:
            await c.close()
            raise ValueError(f"no window found for app {app!r}")
        c.target_pid = mine[0]["pid"]
    return c


def _tool_screen_text(args: dict) -> dict:
    from . import ocr

    if not ocr.available():
        return _text_result("OCR unavailable — pip install axstream",
                            is_error=True)

    async def go() -> dict:
        c = await _targeted_computer(args.get("app"))
        try:
            g = await c.window_geometry(fresh_shot=True)
            if g is None or not g.fresh_shot or not g.shot_path:
                return _text_result("could not capture the target window",
                                    is_error=True)
            lines = ocr.all_text(g.shot_path)
            head = {"window": g.title, "pid": g.pid,
                    "screenshot_px": list(g.screenshot_size or ()),
                    "lines": len(lines)}
            body = [{"text": h.text, "x": round(h.x), "y": round(h.y)}
                    for h in lines[:400]]
            if len(lines) > 400:
                head["truncated_to"] = 400
            out = [json.dumps(head, ensure_ascii=False)]
            out += [json.dumps(b, ensure_ascii=False) for b in body]
            return _text_result("\n".join(out))
        finally:
            await c.close()

    return asyncio.run(go())


def _tool_find(args: dict) -> dict:
    from . import ocr

    query = (args.get("text") or "").strip()
    if not query:
        return _text_result("text is required", is_error=True)
    if not ocr.available():
        return _text_result("OCR unavailable — pip install axstream",
                            is_error=True)

    async def go() -> dict:
        c = await _targeted_computer(args.get("app"))
        try:
            g = await c.window_geometry(fresh_shot=True)
            if g is None or not g.fresh_shot or not g.shot_path:
                return _text_result("could not capture the target window",
                                    is_error=True)
            hit = ocr.find_text(g.shot_path, query)
            if hit is None:
                return _text_result(json.dumps(
                    {"found": False, "window": g.title,
                     "note": "not rendered as text — icon-only controls need "
                             "coordinates or an ax title"}))
            return _text_result(json.dumps(
                {"found": True, "window": g.title, "text": hit.text,
                 "x": round(hit.x), "y": round(hit.y),
                 "confidence": round(hit.confidence, 2),
                 "click_target": {"text": query},
                 "note": "click via act: {\"op\":\"act\",\"do\":\"click\","
                         "\"target\":{\"text\":...}} — re-verified at click time"},
                ensure_ascii=False))
        finally:
            await c.close()

    return asyncio.run(go())


def _tool_check(args: dict) -> dict:
    from . import ocr

    query = (args.get("text") or "").strip()
    if not query:
        return _text_result("text is required", is_error=True)
    if not ocr.available():
        return _text_result("OCR unavailable — pip install axstream",
                            is_error=True)

    async def go() -> dict:
        c = await _targeted_computer(args.get("app"))
        try:
            hit = None
            window = None
            for delay in (0.0, 0.4, 0.8, 1.2):
                if delay:
                    await asyncio.sleep(delay)
                g = await c.window_geometry(fresh_shot=True)
                if g is not None and g.fresh_shot and g.shot_path:
                    window = g.title
                    hit = ocr.find_text(g.shot_path, query)
                    if hit is not None:
                        break
            return _text_result(json.dumps(
                {"visible": hit is not None, "window": window,
                 **({"matched": hit.text} if hit else {})}, ensure_ascii=False))
        finally:
            await c.close()

    return asyncio.run(go())


def _tool_act(args: dict) -> dict:
    from .spec import validate_op

    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        return _text_result("ops must be a non-empty list of spec ops",
                            is_error=True)
    for i, op in enumerate(ops):
        ok, err = validate_op(op)
        if not ok:
            return _text_result(f"ops[{i}] invalid: {err}", is_error=True)

    from .driver import DriverComputer
    from .replay import run_actions

    lines: list[dict] = []

    async def go() -> int:
        computer = DriverComputer()
        await computer.connect()
        await computer.fast_cursor()
        try:
            return await run_actions(ops, computer, emit=lines.append)
        finally:
            await computer.close()

    code = asyncio.run(go())
    return _text_result("\n".join(json.dumps(l, ensure_ascii=False) for l in lines),
                        is_error=code != 0)


def _handle_tool_call(name: str, args: dict) -> dict:
    if name == "verify_macro":
        return _tool_verify(args)
    if name == "screen_text":
        return _tool_screen_text(args)
    if name == "find":
        return _tool_find(args)
    if name == "check":
        return _tool_check(args)
    if name == "act":
        return _tool_act(args)
    if name == "list_macros":
        return _tool_list_macros()
    if name == "replay_macro":
        return _tool_replay(args)
    if name == "read_macro":
        return _tool_read(args)
    if name == "write_macro":
        return _tool_write(args)
    return _text_result(f"unknown tool {name!r}", is_error=True)


def serve() -> int:
    """Blocking stdio loop: one JSON-RPC message per line."""
    out = sys.stdout
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue  # not ours to answer — no id to attach an error to
        msg_id = msg.get("id")
        method = msg.get("method") or ""
        if method.startswith("notifications/"):
            continue
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "axstream", "version": _version()}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                result = _handle_tool_call(params.get("name") or "",
                                           params.get("arguments") or {})
            except Exception as e:  # noqa: BLE001 - a tool bug must become a
                # tool error, never kill the server mid-session
                result = _text_result(f"tool crashed: {e}", is_error=True)
        elif method == "ping":
            result = {}
        else:
            if msg_id is not None:
                out.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                      "error": {"code": -32601,
                                                "message": f"unknown method {method!r}"}})
                          + "\n")
                out.flush()
            continue
        if msg_id is not None:
            out.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                  "result": result}, ensure_ascii=False) + "\n")
            out.flush()
    return 0
