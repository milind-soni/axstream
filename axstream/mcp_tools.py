"""MCP tool schemas — the data half of the server.

Pure declarations: names, descriptions, input schemas. The handlers that
back them live in mcp.py. Split out so mcp.py is protocol + dispatch, not
a 350-line wall of JSON.
"""

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
        "name": "begin_capture",
        "description": (
            "Start capturing a novel Codex native computer-use workflow. "
            "Returns a capture id, trace path, and a short Node snippet that "
            "wraps the already-initialized `sky` object. Continue using "
            "native computer use normally; successful actions, AX state used "
            "to choose element_index targets, and source screenshot dimensions "
            "for coordinate clicks/drags are appended to the trace. Call "
            "get_app_state immediately before those actions. The result also "
            "includes node_teardown; run it when the "
            "first run ends so later native actions are not recorded. After "
            "the task succeeds, call compile_capture. This is "
            "the explicit bridge between Codex computer use and Axstream — "
            "cua-driver's recorder cannot observe `sky` calls by itself."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "short task/macro name"},
                "description": {"type": "string"},
                "when_to_use": {"type": "string"},
            },
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "compile_capture",
        "description": (
            "Compile a successful Codex native computer-use capture into a "
            "normal parameterized .axstream macro. Refuses unsupported or "
            "lossy native actions instead of silently saving a partial "
            "workflow. `slots` maps slot names to captured `value`, optional "
            "description, and optional example. `terminal_assert` is a normal "
            "Axstream target such as {\"text\":\"Brief: {topic}\"}; include "
            "one so the resulting macro can pass verify_macro."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "capture_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "when_to_use": {"type": "string"},
                "slots": {"type": "object"},
                "terminal_assert": {"type": "object"},
            },
            "required": ["capture_id"], "additionalProperties": False,
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
