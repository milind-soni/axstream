# axstream

**A streaming action language for computer-use agents.** An LLM emits actions
as JSONL — one JSON object per line — and the executor performs each action
the moment its newline arrives, while the model is still generating. The
newline is the commit signal: a half-generated action can never fire.

**→ [Read the spec: SPEC.md](SPEC.md)** (axstream-spec 0.1) · **→ [Docs: axstream.dev](https://axstream.dev)**

```spec
{"op":"act","do":"open","target":"Notes"}
{"op":"act","do":"wait","ms":500}
{"op":"act","do":"type","text":"remember to buy milk"}
{"op":"done","status":"success"}
```

## Why

Today's computer-use loops wait for the full model response, act once,
screenshot, and re-prompt — every step pays full decode plus observation.
axstream reads the **accessibility tree** (text, ~150ms scoped) instead of
pixels, overlaps execution with decode, and only re-observes at explicit
`observe` barriers. A burst of N actions costs ~max(decode, execution) instead
of N × (decode + observe).

Measured on the reference implementation: streaming execution saves **37%
wall-clock** vs wait-then-act; a full plan from a fast LLM lands in **~0.4s**;
a learned command replays in **~100ms with no LLM** at **94% end-to-end
accuracy** ([open fine-tuned 350M matcher](https://huggingface.co/milsoni201/lfm25-350m-axstream-matcher), held-out eval).

## Three speeds, one language

- **Instant** — commands you've used before replay directly (frecency-ranked,
  slot-parameterized, guarded against the live screen). No LLM.
- **Fast** — novel commands streamed by an LLM over the AX tree. Every success
  is captured into the instant tier.
- **Fallback** — AX-dead apps via computer use.

## Run it

```sh
git clone https://github.com/milind-soni/axstream && cd axstream && uv sync

# dry demo — no keys, no server; proves the streaming overlap
uv run python demo_dry.py

# set up the local pieces once (full walkthrough: axstream.dev/docs/quickstart)
brew install llama.cpp
curl -L -o ~/models/lfm25-350m-axstream-Q4_K_M.gguf \
  "https://huggingface.co/milsoni201/lfm25-350m-axstream-matcher/resolve/main/lfm25-350m-axstream-Q4_K_M.gguf"
llama-server -m ~/models/lfm25-350m-axstream-Q4_K_M.gguf --port 8791 -ngl 99 -c 4096 --no-webui
/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"   # executor (grant Accessibility)

# start everything with defaults and listen (auto-starts the matcher if down)
uv run axstream up                # type commands
uv run axstream up --voice        # speak them (uv sync --extra voice)

# or piece by piece
uv run axstream --doctor
uv run axstream "launch safari"

# tests
uv run pytest tests
```

The command above downloads the open
[axstream-matcher](https://huggingface.co/milsoni201/lfm25-350m-axstream-matcher)
(94% e2e vs the base model's 47%; misses fall back to the LLM tier).
`AXSTREAM_TINY_URL` overrides the matcher endpoint.

## File macros: agents author, axstream replays

A macro is a plain text file a coding agent (Claude Code) can write, diff,
and refine — a one-line JSON header, then axstream-spec ops, one per line.
Files live in `./.axstream/macros/*.axstream` (project) and
`~/.axstream/macros/` (user); slot placeholders are `{slot_name}` inside
string args (same templating as the macro store).

```
{"name":"new_note_titled","description":"open a new note and type a title","when_to_use":"user wants a fresh note with given text","slots":{"title":{"description":"the note title","example":"standup"}},"provenance":{"source":"hand-written","created":"2026-07-22"}}
{"op":"act","do":"open","target":"Notes"}
{"op":"act","do":"key","keys":["cmd","n"]}
{"op":"act","do":"wait","ms":400}
{"op":"act","do":"type","text":"{title}"}
```

```sh
axstream list                                              # what's available
axstream replay new_note_titled --slots '{"title":"standup"}' --dry   # inspect
axstream replay new_note_titled --slots '{"title":"standup"}'         # execute
```

Replay runs through **cua-driver** (background, pid-addressed — the reliable
edge) and prints one JSON progress object per action. Clicks resolve down a
four-rung ladder — never a raw desktop-scope click that would steal the
user's mouse:

1. **AX element** — the target's label fuzzy-resolves against a fresh window
   snapshot and the element itself is clicked (`click(pid, window_id,
   element_index)`: no cursor move, no focus steal, works on background
   windows).
2. **OCR text anchor** — `{"text": "New Note"}` (or the ax title) is found
   as rendered text in a fresh window screenshot via on-device Apple Vision
   (~25ms; `pip install 'axstream[ocr]'`) and clicked at the hit — a
   target-verified click even when the AX tree can't see the element.
3. **Visual patch anchor** — a small grayscale crop of the control (learned
   once via `replay --learn`; `pip install 'axstream[patch]'`) is re-located
   near its recorded spot by template match: raw grayscale first, Canny
   edges second so a dark↔light theme flip still matches. This verifies
   **icon-only** targets that OCR cannot see. Learning refuses controls that
   aren't visually unique on screen (a lock icon repeated down a list would
   replay onto the wrong row) — refusal is honest, not a failure.
4. **Window-relative pixel** — recorded coordinates remap edge-anchored into
   the live window when the macro carries recorded window bounds (header
   `"window": {x,y,w,h}` or per-op `"win": {fx,fy,w,h}`), surviving both
   window moves and resizes; a wildly different window **refuses** rather
   than clicking blind. Pixel clicks skip the AX element walk entirely
   (window geometry via a max_elements=1 snapshot + a size-keyed cache), so
   they run in ~a second instead of several.

Each line reports which rung ran (`"via": "ax_element" | "ocr_anchor" |
"patch_anchor" | "window_pixel"`) plus the driver's own delivery echo
(`"driver_path"`, `"effect"`); the final summary lists `unverified_steps`
for any click whose target was never verified. `replay --learn` runs a
macro once while capturing patch anchors for every click that lacks one and
saves them back into the file — later replays of those steps are verified
instead of blind.

The header is optional — a raw recorded draft (e.g. a SupaMaus export, ops
only or a provenance-only header) replays as-is. On any assert/act failure the
exit code is non-zero and the last line is the **handoff point** for the agent:

```json
{"failed_at": 2, "op": {"op": "act", "do": "click", "target": {"ax": {"title": "Save"}}}, "reason": "could not resolve target ...", "completed": 2}
```

The agent workflow: Claude writes or refines the file → `axstream replay
... --dry` to check the resolved plan → `axstream replay ...` to execute →
on failure, read `failed_at` and take over (or fix the macro) from that exact
op. Format details in `axstream/macrofile.py`; replay semantics in
`axstream/replay.py`.

## Integrate your STT

You own audio → text; axstream owns text → action. Send the final utterance,
get an executed action or a fast explicit refusal to route to your fallback:

```python
from axstream import Session

session = await Session().connect()
result = await session.handle("launch safari")
# {"tier": "instant", "template": "open_app", "slots": {"app": "safari"}, "status": "done", ...}
```

Or as a pipe (no Python on your side): `your-stt | python -m axstream --stdin`.
Verify setup with `python -m axstream --doctor`. Full contract:
[axstream.dev/docs/integrate](https://axstream.dev/docs/integrate).

axstream does **not** bundle its executor or model server — they're pluggable
local processes (cua-driver / computer-server / your own `Computer`-shaped
backend; any OpenAI-compatible server for the matcher). `--doctor` tells you
what's missing and how to install it.

## Spec properties

- **Line = commit unit.** Truncation-safe by construction.
- **No dedup.** Identical lines both execute — repetition is meaningful.
- **Late binding.** `{"ax":{"role":"AXButton","title":"Save"}}` resolves
  against the live tree right before the click; `assert` + `observe` bound the
  speculation horizon.
- **Risk classes.** `"risk":"risky"` marks hard-to-undo actions; policy gates
  them (`--no-risky`).

## For coding agents (Claude Code / Codex)

One command wires axstream into every coding agent on the machine — a skill
plus an MCP server, idempotently:

```sh
axstream install
```

The agent gets **accelerator primitives** (`screen_text` — the window as OCR
text with coordinates, ~200ms, instead of a screenshot; `find` — a
click-ready target; `act` — a whole batch of verified actions in one call;
`check` — a ~250ms outcome poll) plus the **macro flywheel**
(`list/replay/read/write/verify_macro`). Any task the agent does once
compiles into a macro that replays in seconds with zero model calls — and is
never trusted until the verify gate (`axstream verify <name>`: one live
replay whose terminal assert must pass) stamps it. `axstream bench <name>`
reports p50/p95 per op. Full guide: [axstream.dev/docs/agents](https://axstream.dev/docs/agents).

## Layout

```
SPEC.md              the canonical action language (CC BY 4.0)
axstream/
  compiler.py        newline-committed stream compiler
  executor.py        pipelined executor + zoxide-tier replay
  ax.py              AX-tree observation, terse summaries, fuzzy resolve
  computer.py        computer-server WebSocket client (+ MockComputer)
  driver.py          cua-driver backend (background, pid-addressed delivery)
  geometry.py        window-relative click remapping (moves + resizes)
  ocr.py             Apple Vision text anchors / assertions ([ocr] extra)
  patch.py           visual patch anchors for icon-only controls ([patch] extra)
  gate.py            verify-before-store gate + task-family dedup
  macros.py          frecency-ranked parameterized macro store
  macrofile.py       file-based macros (.axstream): header + spec JSONL
  replay.py          replay / list / bench — the agent-facing CLI
  mcp.py             MCP server: accelerator primitives + macro tools
  install.py         `axstream install` — skill + MCP wiring for agents
  skills/            the packaged Claude Code / Codex skill
  tiny.py            local tiny-model matcher (schema-constrained)
  capture.py         parameterize a successful run into a macro
  llm.py / prompt.py / runner.py / spec.py
demo_dry.py          no-keys streaming-overlap demo
docs/                axstream.dev (Fumadocs)
```

## License

Reference implementation: MIT ([LICENSE](LICENSE)). Spec: CC BY 4.0.
