# axstream

**Fast, verified computer use that compiles.** Your coding agent does a macOS
UI task once; axstream saves it as a small text macro that replays in seconds
— no screenshots, no per-step reasoning, and never trusted until it proves its
own outcome. The first run is agent-speed; every run after is ~100ms per
action, verified, or an honest refusal.

**→ [Docs](https://axstream.dev)** · **→ [The spec](SPEC.md)** · macOS

## In four commands

```sh
uv tool install axstream          # or: pip install axstream
axstream install                  # wire the skill + MCP into Claude Code / Codex
axstream --doctor                 # check the driver + permissions
axstream replay <macro>           # a saved task, in seconds
```

The one prerequisite is the [cua-driver](https://github.com/trycua/cua)
daemon (`--doctor` tells you how). Then your agent gets the flywheel: **do a
task once → it's saved → it replays instantly, forever.**

```
{"name":"new-note","description":"a titled Apple Note","slots":{"title":{"example":"standup"}}}
{"op":"act","do":"open","target":"Notes"}
{"op":"act","do":"key","keys":["cmd","n"]}
{"op":"act","do":"type","text":"{title}"}
{"op":"assert","target":{"text":"{title}"}}
```

## Why it's fast — and why it's trusted

A coding agent drives a Mac the expensive way: screenshot → reason → one click
→ screenshot again, seconds and tokens per step. axstream replaces the repeat
of any task with a deterministic macro: clicks resolve through a **verified
ladder** (accessibility element → OCR text anchor → visual patch → window-
relative pixels) and every macro ends with an **assert** that only passes when
the task actually happened. A macro that can't prove its outcome never earns
the `verified` stamp — following
[PreAct](https://arxiv.org/abs/2606.17929), whose result is that cached-replay
systems collapse without exactly this gate.

Measured against a coding agent's native computer use on the same repeat
tasks: **34–78× faster** at the execution layer, deterministic at p95,
verified-or-refused every time.

## Install

**As a Claude Code plugin** (skill + MCP tools + slash commands, one line):

```sh
claude plugin marketplace add milind-soni/axstream
claude plugin install axstream@axstream
```

**Any other agent** (Codex, Cursor, Windsurf — via the Agent Skills standard):

```sh
npx skills add milind-soni/axstream
```

**Or the package directly:**

```sh
uv tool install axstream          # from PyPI — or: pip install axstream
axstream install                  # wire the skill + MCP into Claude Code / Codex
```

One prerequisite for live execution either way: the cua-driver daemon
(`/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"` + grant
Accessibility). `axstream --doctor` checks everything.

After install, your agent gets `/axstream` (status), `/axstream-teach`
(compile a task into a macro), `/axstream-stats` (benchmark), the fast
primitives (screen-as-text, batched verified actions), and the macro
flywheel.

## Run it (from a clone, for hacking)

```sh
git clone https://github.com/milind-soni/axstream && cd axstream && uv sync
uv run axstream --doctor
uv run axstream replay <macro> --slots '{"...":"..."}'
uv run pytest tests            # 133 tests
```

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
   (~25ms; installed by default) and clicked at the hit — a
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
(`list/replay/read/write/verify_macro`) and the **native capture bridge**
(`begin_capture/compile_capture`). Any task the agent does once
compiles into a macro that replays in seconds with zero model calls — and is
never trusted until the verify gate (`axstream verify <name>`: one live
replay whose terminal assert must pass) stamps it. `axstream bench <name>`
reports p50/p95 per op. Full guide: [axstream.dev/docs/agents](https://axstream.dev/docs/agents).

Codex native computer use runs through its own `sky` runtime, so simply
enabling cua-driver trajectory recording cannot observe those actions.
`begin_capture` returns a tiny JavaScript facade for the initialized `sky`
object. It logs successful native calls plus the accessibility state used to
choose each `element_index`; `compile_capture` then converts that real trace
into semantic AX targets, deterministic keys/types, slots, and a normal
`.axstream` macro. Unsupported steps are refused rather than silently omitted.
Full AX text is persisted only for actions that need `element_index` binding;
ordinary observations retain a short window summary.
Coordinate clicks and drags persist only the source screenshot dimensions,
not its pixels. The compiler converts those points to window fractions, so
replay scales correctly from native `sky`'s 1× app image to Retina driver
pixels and survives ordinary window resizing. Missing dimensions make
compilation fail honestly instead of producing a likely-wrong gesture.
Run the returned teardown snippet when the first run finishes; capture JSONL
lives under `~/.axstream/captures/` for inspection and may contain UI text
that native computer use observed.

Use `wait_until` in authored macros when visible text or an AX target signals
readiness. It observes immediately and polls only until the condition appears,
avoiding the fixed sleeps that often dominate an otherwise instant replay.

## Layout

```
SPEC.md              the canonical action language (CC BY 4.0)
axstream/
  macrofile.py       the .axstream format: header + spec JSONL, slots
  spec.py            op catalog + validate_op
  driver.py          cua-driver backend (background, pid-addressed delivery)
  replay.py          run_actions + the replay / list CLI
  act.py             the click resolution ladder (AX -> OCR -> patch -> pixel)
  bench.py           `axstream bench` — p50/p95 per op across runs
  see.py             window-as-text perception behind the MCP see tools
  ocr.py             Apple Vision text anchors + assertions
  patch.py           visual patch anchors for icon-only controls ([patch] extra)
  geometry.py        window-relative click remapping (moves + resizes)
  check.py           verify + wait: asserts, wait_until, stability, scroll
  gate.py            verify-before-store gate + task-family dedup
  ledger.py          run receipts -> `axstream stats`
  ax.py              AX-tree observation + fuzzy element/app-name resolve
  mcp.py             MCP server (protocol + dispatch); mcp_tools.py = schemas
  codex_capture.py   compile captured Codex native `sky` traces into macros
  phone.py           iPhone Mirroring backend (OCR eyes, raw-HID hands)
  install.py         `axstream install` — skill + MCP wiring for agents
  skills/            the packaged Claude Code / Codex skill (source of truth;
                     the plugin copy at ../skills is a symlink to it)
```

## License

Reference implementation: MIT ([LICENSE](LICENSE)). Spec: CC BY 4.0.
