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
demo_live.py       LLM tier vs live computer-server
demo_learn.py      THE zoxide-tier demo: slow-first (LLM) vs instant-second (replay)
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
  capture + `executor.replay()` + `demo_learn.py`. Verified end-to-end on a
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
slot-filled text into TextEdit reliably. `demo_replay.py` is that working demo.
Gotcha: `launch_app` returns the pid in PROSE text ("...(pid 6821)..."), parsed
by `DriverComputer._extract_pid`; `tool()` first arg is positional-only to
avoid colliding with a `name=` argument.

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
cd axstream && uv run python demo_learn.py

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
