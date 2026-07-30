# axstream — Handover

Context for an agent picking this up. Read this, then `SPEC.md`, then skim
`axstream/` (the reference runtime). Date of this handover: 2026-07-19.

---

## 1. What axstream is (the one-paragraph version)

A **streaming action language for computer-use agents**. An LLM emits actions
as JSONL — one JSON object per line — and an executor performs each the moment
its newline arrives, *while the model is still generating*. The newline is the
commit signal, so a half-generated action can never fire. It's the substrate
for a bigger vision: **voice → action, as fast as dictation**, delivered by a
three-speed system (below). Open-core: spec + runtime are open (MIT / CC BY 4.0);
the trained model + dataset stay proprietary.

- Public repo: `github.com/milind-soni/axstream` (PUBLIC)
- Docs: `axstream.dev` (Fumadocs, Vercel, `supa-maus` team scope)
- Spec: `SPEC.md` (axstream-spec 0.1)

## 2. The vision (why this exists)

Voice-to-action is slow today. Latency has three sources, each with a fix:
1. **Vision models** → use the **accessibility tree** (text), not pixels.
2. **Waiting for the model to finish** → **stream the actions** and execute as
   they arrive.
3. **Figuring out what you meant** (who's "Agni", which app, what tone) → the
   real killer; the fix is **don't call an LLM for what you've done before**.

This yields the **three-speed system**, all speaking the same axstream language:
- **Instant** — known commands replay directly, ranked by frecency (the
  zoxide model), **no LLM**. This is the "zoxide tier".
- **Fast** — novel commands streamed by a small model over the AX tree.
- **Fallback** — the long tail of un-integrated apps via computer use.

The LLM tier *generates* macros; the zoxide tier *replays* them; a (future)
user model *ranks/pre-stages* them. Every LLM success promotes itself into the
instant tier — the system gets faster the more you use it. This is the moat and
the "magic" of the product.

Honest framing (important): **general "do anything by voice" at Wispr-flow
reliability is NOT solved and won't be soon** (computer-use SOTA ~70%). The
reliable, shippable slice is the **zoxide tier** — deterministic replay of
*your* learned commands hits high reliability because it isn't reasoning, it's
replaying. Sell that; treat the LLM/fallback tiers as best-effort.

## 3. Repo layout (the reference runtime)

```
SPEC.md            axstream-spec 0.1 — the canonical action language
README.md          spec-forward overview
axstream/
  spec.py          op/action catalog + validate_op (act/assert/observe/done)
  compiler.py      StreamCompiler — newline-committed, any ``` fence, no dedup
  executor.py      Executor.run_burst (streaming) + Executor.replay (zoxide tier)
  ax.py            Snapshot — AX-tree observation, terse text, fuzzy resolve
  computer.py      Computer — thin WS client to cua's computer-server (+ MockComputer)
  llm.py           raw SSE streaming (Anthropic + OpenAI-compat/Groq). NO litellm.
  prompt.py        the SYSTEM prompt for the LLM tier (one-burst biased)
  runner.py        run_task — the observe→stream→execute burst loop
  macros.py        zoxide tier: frecency-ranked parameterized macro store
  tiny.py          TinyMatcher — LFM2.5-350M, JSON-schema constrained match+slots
  capture.py       parameterize a successful LLM run into a macro
demo_dry.py        no-keys streaming-overlap demo
(demo_live/learn/replay/voice removed 2026-07-30 — superseded by the
 `axstream` CLI: replay / up / up --voice / bench)
tests/             13 tests (compiler, db-ish, macros) — all green
docs/              Fumadocs site (deploys to axstream.dev; Root Directory=docs)
```

## 4. What's been built and verified

- **The spec + streaming runtime**: compiler, executor, AX observation, LLM
  clients. Streaming execution proven (dry demo: execution fully overlaps
  decode). One-burst planning + `reasoning_effort:"none"` on qwen: full plan in
  ~0.43s.
- **Live execution** against cua's `computer-server` (WebSocket) AND `cua-driver`
  (MCP, background/pid-addressed delivery). AX observation ~150ms scoped.
- **The zoxide tier (just built, `9de7cc5`)**: macro store + tiny matcher +
  capture + `executor.replay()` (demo since removed). Verified end-to-end on a
  MockComputer: **~90ms match → slot-fill → replay**, no LLM.
- **Model choice, researched**: `LFM2.5-350M` (Liquid) is the tiny matcher —
  ~65–100ms/call on Apple Silicon, purpose-built for extraction/tool-use.
  Running locally via `llama-server` (GGUF Q4_K_M at `~/models/`).
- **Docs live** at axstream.dev (dark/Vercel-black, 4 pages: Intro / Spec /
  Quickstart / Roadmap), Git-connected auto-deploy.

## 4b. Execution backend: use cua-driver, NOT computer-server (proven 2026-07-19)

The reference `Computer` (computer-server WebSocket) is **flaky for execution**:
the unscoped `get_accessibility_tree` hangs (full-desktop walk), Notes is
AX-hostile (creates an empty note, typing goes nowhere), and focus races drop
keystrokes. **cua-driver is the reliable executor edge** — `axstream/driver.py`
`DriverComputer` (MCP over stdio to `~/.local/bin/cua-driver`). It delivers
keys/clicks to a specific pid in the **background** (no focus race). Verified
live: typing lands with `"verified": true`, and the full instant tier
(tiny match -> `executor.replay` -> DriverComputer) types the correct
slot-filled text into TextEdit reliably (the standalone demo for this was
removed 2026-07-30; `axstream replay` is the surviving surface).
Gotcha: `launch_app` returns the pid in PROSE text ("...(pid 6821)..."), parsed
by `DriverComputer._extract_pid`; `tool()` first arg is positional-only to
avoid colliding with a `name=` argument.

DONE 2026-07-27 (branch fix/replay-ax-path): **file-macro replay clicks no
longer use desktop-scope pixel clicks** (scope="desktop" steals the user's
real mouse, races their pointer, and misses when windows moved since
recording). `replay.py:_click_via_ladder` resolves click/double_click through
a strict ladder: (1) **AX element** — fresh `DriverComputer.window_snapshot()`
(tree-only get_window_state) before EVERY click (the driver's element cache is
per-snapshot), fuzzy label match via `ax.resolve_window_element` (same scorer
as `Snapshot.resolve_element`) -> `click(pid, window_id, element_index)` =
AXUIElementPerformAction: no cursor move, no focus steal, works on
background/off-Space windows. (2) **window-local pixel** — no AX match (or the
AX action hard-errors, e.g. kAXErrorActionUnsupported on Notes list cells):
re-snapshot WITH a screenshot (its dims define the driver's pixel space) and
convert recorded global screen coords via `driver.window_pixels_from_screen`
((g − bounds.origin) × screenshot_px/logical_bounds — the driver divides that
scale back out and re-adds the origin), then pid-addressed
`click(pid, window_id, x, y)`. (3) neither -> the failure handoff JSON,
reason distinguishing "label not found in AX tree" vs "no window". Progress
lines carry `via: ax_element|window_pixel` + the driver's own `path`/`effect`
echo. Live-verified on the Notes macro: full run exits 0 entirely in the
BACKGROUND (user held focus on another Space throughout); `driver_path:"ax"`
on labeled targets. Driver-contract gotchas learned: double_click's schema
SAYS x/y are screen coords but its implementation window-localizes them
exactly like click when window_id is passed; Notes' toolbar buttons ("New
Note") expose NO AXTitle/description, so they can only pixel-fallback;
`suspected_noop` effects are surfaced but NOT auto-escalated (the press
dispatched — a pixel retry could double-act); transient title-'' tooltip
windows sit ABOVE the main window in z (filtered in `_front_window`), and
window screenshots fail transiently right after a click (per-window dims
cache + one settle-retry cover it). Only the streaming/burst tier still uses
`DriverComputer.click` (desktop scope).

DONE 2026-07-19: observation ported (`DriverComputer.ax_tree()` — list_apps
active -> list_windows max-z window -> get_window_state, frames are already
screen-global) and the WHOLE flywheel now runs on the driver through
`Session.handle`: no-match -> run_task (LLM tier) -> capture.debind (late-bound
role/title targets, never per-burst ids) -> learn -> instant replay with live
re-binding. Live proof: "create a new tab in firefox" fast-tier 29s learned ->
instant 103ms match/replay done. Fast-tier LLM: OpenRouter preferred (Groq free
tier TPM-crawls to 2min+). Known gaps: compound utterances partial-match single
macros (matcher grabs "open firefox" from "open firefox and create a new tab",
drops the rest — data round 2 hard-negatives); fast tier ~29s needs trimming
(observation size, parameterize second call).

## 4c. File macros + agent-facing replay CLI (2026-07-22, branch feature/file-macros)

**The reshape**: axstream is now agent-centric first. In the primary workflow
a coding agent (Claude Code) — not the tiny matcher — authors, refines, and
invokes macros, as plain files. The tiny matcher (tiny.py, LFM2.5) is
**demoted to optional**: it stays fully intact and the voice tier still uses
it, but it is no longer the front door — it's reserved for the future voice
tier where an utterance must be matched without an agent in the loop. Nothing
in tiny.py/llm.py/session.py changed.

What landed:
- `axstream/macrofile.py` — the `.axstream` file format: an optional one-line
  JSON header (name, description, when_to_use, slots {name: {description,
  example}}, provenance {source: supamaus-recording|llm-run|hand-written,
  capture_id?, created}, optional matcher `examples`) followed by spec-0.1
  JSONL ops. `#` comments + blank lines allowed. Slot syntax is the EXISTING
  `{slot_name}` single-brace templating (macros._fill) — unified, no second
  syntax. Header may alternatively live in a `<name>.json` sidecar. Dirs:
  `./.axstream/macros/` (project) then `~/.axstream/macros/` (user).
- `axstream/replay.py` + subcommands in `__main__.py`:
  `axstream replay <name|path> [--slots '{"k":"v"}'] [--dry]` and
  `axstream list [--json]`. Replay executes via **DriverComputer** (§4b —
  never computer-server), emits one JSON progress line per action, and on
  failure exits 1 with a final
  `{"failed_at", "op", "reason", "completed"}` line — the agent's handoff
  point. Click targets may carry BOTH coords and an AX label (the upcoming
  SupaMaus draft-export shape): AX resolves first (fuzzy, live tree, one
  refresh via Executor._refresh_and_resolve), coords are the fallback;
  `"via"` on each line says which was used. Raw header-less drafts replay
  as-is. `done` stops a replay; `observe` is a no-op in file replay.
- **Frecency store untouched** (macros.py byte-identical). Making it an index
  over files would have meant rewriting MacroStore's merge-on-save + the
  session/tiny read paths mid-flight; instead `macrofile.to_macro/from_macro`
  bridge the two representations (file → store for the matcher, captured
  macro → file for agents). Wiring file macros into `Session` seeding is a
  small follow-up if wanted.
- Tests: 43 total (13 existing all still green + 30 new: round-trip, slot
  fill, discovery, --dry, failure-JSON shape, ax-first/coords-fallback
  resolution against MockComputer). Live-verified through the real
  cua-driver: wait-op replay exits 0; failing assert prints the handoff JSON
  and exits 1.

## 4d. FAST REPLAY LADDER — SHIPPED (2026-07-30)

The reliability/latency overhaul that Phase 1+2 of the July-30 plan called
for. Baseline on `notes-create-bold-note`: 5/5 clicks blind, 2.7–4.5s each,
~21s total. Now: ~1.3s per pixel click, ~11.5s total, and clicks are
target-verified whenever a rung above pixels resolves.

**The new click ladder** (`replay._click_via_ladder`):

1. **AX element** — unchanged (full `max_elements=500` walk, needed for
   `element_index`).
2. **OCR text anchor** (`ocr.py`, NEW) — `target.text` (or the ax title) is
   located in a fresh window screenshot via Apple Vision through pyobjc
   (`pip install 'axstream[ocr]'`, lazy import, soft-fails without it).
   Fast pass ~25ms, accurate retry ~550ms on miss. The hit is clicked in
   screenshot-pixel space — same PNG that defines the driver's pixel-click
   contract, zero conversion — and counts as target-VERIFIED. Note: finds
   rendered TEXT only; icon-only toolbar buttons (Notes "New Note") are
   invisible to OCR too — those need `key` shortcuts or coordinates.
3. **Window-relative pixel** (`geometry.py`, NEW) — `target.win`
   `{fx,fy,w,h}` (or derived at load time from a header `"window":{x,y,w,h}`
   = recorded window bounds) remaps edge-anchored into the live window:
   survives moves AND resizes; >1.6x size change **refuses** instead of
   clicking blind (`GeometryMismatch`). Plain global x/y stay translate-only.

**Why pixel clicks got 3x faster** (`driver.window_geometry`, NEW): a pixel
click needs no element walk — `get_window_state(max_elements=1,
screenshot_out_file=...)` short-circuits the AX traversal at one node
(measured 126–206ms vs ~1s at 500 on Notes) while still registering the
driver's per-pid downscale ratio consistently. Screenshot dims are cached
per (pid, window_id) KEYED ON WINDOW SIZE — dims and ratio depend only on
size, so the cache survives window moves; a resize re-snapshots. Repeat
pixel clicks cost one ~14ms list_windows + the driver click.

**Cursor motion** (`driver.fast_cursor`): clicks pass
`session="axstream-replay"` and replay sets that cursor instance to
`glide_duration_ms=50, dwell_after_click_ms=0` once per run — the default
speed-based glide costs ~2s/click of pure animation. Other driver clients
keep their pretty cursor.

**Honesty accounting**: `unverified_steps` now means "target never
verified" — `window_pixel` clicks and `suspected_noop` AX presses. AX- and
OCR-resolved clicks no longer count as blind (the driver can never verify a
click's downstream effect; target-presence is the verification we can give).

**The remaining 1s per click is the DRIVER's**: click.rs runs a
WindowChangeDetector poll after every action that early-exits only when a
window change appears — a quiet click always pays the full
`DEFAULT_TIMEOUT` 1000ms. The public escape (`_skip_window_change_detection`)
is transport-reserved and stripped from public callers
(`sanitize_reserved_args`). FOLLOW-UP: add a public
`observe_window_changes:false` arg to click/double_click/type/key in
cua-driver and rebuild — repo checkout was at 0.12.4-era while the installed
daemon is 0.12.6, so sync first. That takes a pixel click to ~0.3s.
(Bonus found in click.rs: a background left pixel click that lands on an
AX-pressable element returns EARLY via the PX-AX hit-test — no 1s poll —
so some pixel clicks are already ~0.3s, data-dependent.)

**Bubble-cursor snap** (`ocr.nearest_text`, same day): when a remap is
`anchored` (window resized since recording → the point is APPROXIMATE), the
click snaps to the nearest OCR text line within ~3% of the window width —
but only when unambiguous (runner-up ≥1.5x farther); a wrong snap is worse
than an honest near-miss, so dense neighborhoods stay untouched. Exact-size
replays never snap. `all_text` on a full Notes window = 61 lines / ~170ms,
paid only on the anchored path. Progress line gets `"snapped_to"`.
Coordinate math itself was verified pixel-perfect via the driver's
`debug_image_out` crosshair — "clicks not landing" is (1) stale recordings,
(2) UI drift between observe and act, (3) apps ignoring background synthetic
clicks — never the transform. Driver gotcha found live: a reused `session`
id can be in the "ended" state and REJECTS tool calls — session ids are now
unique per process (`axstream-<pid>`).

**Later the same day — accelerator tools + the PreAct gate** (see the
memory file / commit message for the full story):

- **`axstream install`** — one-command onboarding: copies the packaged
  skill (now at `axstream/skills/axstream/SKILL.md`, ships in the wheel)
  into `~/.claude/skills` + `~/.agents/skills`, registers the MCP server in
  Claude Code + Codex. Idempotent.
- **`axstream mcp`** (`mcp.py`, hand-rolled stdio JSON-RPC, no SDK):
  macro tools (list/replay/read/write/verify) PLUS accelerator primitives
  that complement a host agent's native computer use — `screen_text`
  (window as OCR text + coords, ~200ms, replaces screenshots), `find`
  (click-ready target), `check` (~250ms outcome poll), `act` (a BATCH of
  verified ops in one call). All live-verified in Claude Code and Codex
  (Codex gotcha: its sandbox blocks the driver socket — needs approved
  execution; documented in the skill).
- **OCR outcome asserts** — `{"op":"assert","target":{"text":...}}` polls
  Vision ~250ms; `demo-web-search` ends with one.
- **`axstream bench <macro> --warmup N --runs N`** — p50/p95 per op.
  Measured: the two ~1.0s `key` ops are 71% of a verified 2.9s run — the
  driver poll again.
- **The verify-before-store gate** (`gate.py`, from the PreAct paper
  arXiv 2606.17929 — its one structural finding, worth 1.75-2.6 tasks):
  `axstream verify <name>` / `verify_macro` = ONE live replay whose
  TERMINAL ASSERT must pass -> `verified` stamp in the header
  (content-hashed; editing the actions demotes to stale; `list_macros`
  shows the state). Refuses `risk:"risky"` macros — verification
  re-executes the task (PreAct L6), so irreversible macros are verified by
  humans. `write_macro` upserts by task-family SIGNATURE (action shapes
  with values blanked): a same-task twin under another name is archived to
  `.archive/`, not left to rot. PreAct also validates choices already
  made here: flat scripts + per-step checks ≈ state machines (their
  ablation), embedding-level matcher suffices, cache-miss fallback
  required; their OOD result is NEGATIVE (−11pp) — keep macros narrow.

**Also shipped**: `skills/axstream/SKILL.md` — the Claude Code / Codex
agent skill teaching the list → dry → replay → handoff loop and the macro
format (symlinked into `~/.claude/skills/axstream`). Spec now validates
`target.text` and `target.win`; tests 59 → 82.

**Live finding (2026-07-30)**: the recorded `notes-create-bold-note` pixel
click for "New Note" no longer lands on the button (window geometry changed
since the 07-26 recording; the draft has no recorded window bounds). Exactly
the failure class the header `"window"` fix addresses — SupaMaus's exporter
should start emitting recorded window bounds in the header (one line), and
capture hygiene should prefer `key cmd+n` over toolbar clicks.

## 4e. VISUAL PATCH ANCHORS — SHIPPED (2026-07-30)

Closes the ladder's icon gap: OCR verifies only RENDERED TEXT, so icon-only
controls fell through to blind `window_pixel` clicks. New rung between OCR
and pixels (`patch.py` + `replay --learn`, optional `axstream[patch]` =
opencv-headless): a small grayscale crop of the control rides in the op
target (`target.patch`, base64 PNG + fx/fy/sw/sh in SCREENSHOT-pixel space —
same space as the driver's pixel-click contract, zero conversion) and is
re-located near its recorded spot by template match at replay. A hit is
target-VERIFIED, same grade as an OCR hit. `via: "patch_anchor"`.

Thresholds are from live experiments on a real System Settings window
(session 2026-07-30, saved to project memory): raw grayscale ≥0.88 (nothing
changed → ~1.0); Canny-edge ≥0.75 (a dark↔light THEME FLIP kills raw match
~0.2 but edges hold 0.78–0.88 at the true spot — matched second); a known
screenshot-size change rescales the patch first (raw dies at ±10% scale,
rescaled measures ~0.98). Search is LOCAL only (~96px+ around the expected
fraction of the live shot) — a patch is a verifier, not a global finder; a
global search would trade a blind click for a confident wrong one.

The load-bearing part is the CAPTURE-TIME UNIQUENESS GATE
(`patch.capture_patch`): the crop is matched against its own screenshot,
best hit masked, and the runner-up must trail by ≥0.12 — measured: text
labels margin 0.27–0.39, a lock icon repeated down a list margin 0.00 (8
identical hits). Ambiguous or featureless (std<4) controls are REFUSED so a
duplicate-looking anchor can never replay onto the wrong row. Refusal is
silent-honest: the step just stays a blind pixel click.

Learning: `axstream replay <macro> --learn` captures a patch from the
PRE-click screenshot for every ocr_anchor / window_pixel click that lacks
one (ax_element steps skip — no screenshot on that path, and they're already
the most robust) and `macrofile.save_patches` merges fragments back into the
file byte-preserving everything else (header, comments, untouched ops).
Learned even on mid-run failure — completed steps' anchors are valid. Run
`--learn` once while CONFIRMING the macro works: it bakes in whatever the
screen showed (this is the PreAct paper's verify-before-store lesson — an
unverified stored anchor makes later runs worse, arXiv:2606.17929).

Tests 92 → 110 (`tests/test_patch_anchor.py`, synthetic numpy-drawn UI, a
ShotDriver fake that writes real PNGs; skips cleanly without cv2). Suite
green. NOT yet live-tested against a real app replay — next `--learn` run on
`notes-create-bold-note` (after re-recording it, see §4d live finding) is
the proving ground.

## 5. THE TINY-MATCHER FINE-TUNE — DONE (2026-07-19)

The gap is CLOSED. LoRA fine-tune of LFM2.5-350M, trained locally on the M5
with mlx-lm (~25 min), evaluated through the REAL serving path (llama-server +
JSON-schema constrained decoding) on a 387-example held-out test set:

| metric                | base  | tuned |
|-----------------------|-------|-------|
| e2e correct           | 47.3% | 93.3% |
| template acc (pos)    | 64.0% | 97.5% |
| slot exact (given t)  | 72.9% | 97.7% |
| none recall           | 50.0% | 84.3% |
| wrong-match on none   | 50.0% | 15.7% |
| p50 latency           | 104ms |  98ms |

Everything lives in the PRIVATE workspace `../axstream-train` (open-core: the
model + dataset stay out of this repo): `templates.py` (40-template catalog),
`generate.py` (OpenRouter/Groq synthetic gen + cross-family judge filter,
2.6k examples), `evaluate.py` (real-path eval, `--url` to pick server),
`lora_config.yaml` (r=32, ALL projections incl. conv `in_proj`, lr 1e-4).
Tuned GGUF: `~/models/lfm25-350m-axstream-Q4_K_M.gguf` (serving on :8792).

GOTCHA for retrains: `mlx_lm fuse` writes the LFM2 short-conv kernels in MLX
Conv1d layout `(out, k, 1)`; llama.cpp expects HF `(out, 1, k)` and asserts at
load. Fix: restore `conv.conv.weight` tensors from the original HF snapshot
before `convert_hf_to_gguf.py` (LoRA never touches them).

Residual weak spots (test failures): `none` vs `open_url`/`open_folder`
confusion on entity-like utterances, occasional verbatim-copy artifacts
("quit out of spotify" → slot "out of spotify"). The `wrong_match_on_none`
15.7% is why replay guards + risk gating stay mandatory.

## 6. How to run things

```sh
# tiny matcher (required for zoxide tier) — use the TUNED model (93% e2e vs 47% base)
llama-server -m ~/models/lfm25-350m-axstream-Q4_K_M.gguf --port 8791 -ngl 99 -c 4096 --no-webui

# computer-server (required for live execution) — needs Accessibility perms
cd ../cua/libs/python/computer-server && uv run python -m computer_server --port 8765

# the demo (GROQ_API_KEY in axstream/.env)
cd axstream && uv run axstream up   # (demo_learn.py removed 2026-07-30)

# tests
uv run pytest
```

Ports: **8765** computer-server, **8791** tiny model. `.env` holds `GROQ_API_KEY`
and `CLAUDE_API` (gitignored — never commit).

## 7. Key decisions (and why) — don't re-litigate these

- **AX-tree over vision** — speed + the empty macOS-AX niche. Pixels are the
  fallback for AX-dead apps (Electron/Blender/canvas), via `cua-driver`'s zoom.
- **Cascade, not a realtime/speech-native model** — research showed cascades
  beat speech-native on tool-calling latency AND accuracy today. Audio-native
  (Ultravox-style projector onto the trained action model) is the *long-game
  moonshot*, funded by shipping the cascade, not instead of it.
- **No litellm / no heavy SDKs** in the runtime — raw httpx SSE. Keep it small.
- **Open-core**: spec + runtime open; trained model + dataset proprietary,
  in a SEPARATE private repo (never in this one). The cua fork
  (`milind-soni/cua`) is only a staging area for upstream PRs (e.g. the
  scoped-AX-tree patch), NOT the product home.
- **Slot handling is v1, not v2** — a note title is inherently variable, so the
  macro stores a template + `{slot}`, filled fresh from each utterance. The
  scaffolding replays; the slot fills; messy multi-slot commands fall back to
  the LLM tier.
- **Deleted the Swift/Electron voice apps** — they were front-end scaffolding.
  The core (spec/compiler/executor/ax) is what the zoxide tier reuses. A voice
  front-end returns later as an *optional* layer feeding tasks in.

## 8. The roadmap (sequenced)

1. **Fine-tune the tiny matcher** (§5) — makes the demo solid. NEXT.
2. **Live "learn-then-instant" run** on a real Mac (computer-server + real
   commands), record the numbers.
3. **Frecency polish + guard coverage** — the zoxide ranking matters at scale;
   guards (spec's `assert`/`expect`) make replay safe against UI drift. Prefer
   `ax` role/title targets (late-bound) over coordinates so macros survive
   layout changes.
4. **Voice front-end** (optional package) — streaming STT (FluidAudio
   Parakeet-EOU) feeding tasks in; then speculative eager-execution on stable
   partials (act on scaffolding while the user still speaks — spec's `risk`
   classes gate the irreversible `commit`).
5. **The trained action model** (separate private repo) — SFT a 4–8B on macOS
   AX trajectories (public datasets: AgentNet's 5k real macOS trajectories,
   NNetNav, AndroidControl, GUI-360, Mind2Web/AgentTrek → our format), then
   KTO/GRPO in the axstream harness (which is already an RL environment +
   verifier). This replaces the "Fast" tier's Groq dependency with a local
   model. Eval on macOSWorld / MacAgentBench.
6. **User-model pre-staging** (LongNAP/GUMs direction) — predict + pre-warm the
   likely next action so scaffolding is done before you finish speaking.
7. **Audio-native model** — the moonshot (§7 decisions).

## 9. Data / research already done (in memory, don't re-research)

A large research sweep is captured in the user's memory file
`project_streaming_cua_spec.md` (dataset inventory, tiny-model benchmarks,
realtime-API verdict, training recipes, the zoxide-tier design). Highlights:
- Datasets to bootstrap the trained model: **AgentNet** (5k real macOS, MIT),
  NNetNav, AndroidControl, GUI-360 (+failure steps for DPO), Mind2Web/AgentTrek.
  Eval holdouts: macOSWorld, MacAgentBench.
- Tiny models: LFM2.5-350M (winner) > Gemma 3 270M. Apple Foundation Models =
  NOT the free lunch (3B@~30tok/s, cold start, rateLimited hits daemons);
  but macOS 27 `MLXLanguageModel` can run our tuned model under Apple's
  `@Generable` constrained decoding — a future hybrid.
- Constrained decoding is ~free (llguidance ~50µs/token). Prefix-cache the
  static template library so only the transcript prefills.

## 10. Gotchas

- `.env` has secrets — gitignored, verified clean history. Keep it that way
  before any repo goes public.
- The tiny matcher returns `"none"` → route to LLM tier. A wrong `"none"` costs
  one slow call; a wrong *match* could do a wrong action — so tune for recall
  and lean on the guard + confidence, and never auto-run a `risk:risky` replay
  without a check.
- cua-driver MCP framing is **newline-delimited JSON** (not LSP). Driver tool
  quirks: `scroll` uses `amount` (1–50); pixel click coords are window-local
  (use `scope:"desktop"` for screen pixels); `hotkey` needs ≥2 keys.
- Vercel: docs project Root Directory MUST be `docs` (the Next app isn't at
  repo root); domain is under the `supa-maus` team scope.
