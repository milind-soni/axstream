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

## 5. THE OPEN GAP (do this next)

**Base LFM2.5-350M gets templates mostly right but fumbles slot extraction
~1/3 of the time** (e.g. "launch spotify" → correct template `open_app` but
copied an example's slot "safari" instead of "spotify"). This is exactly the
research-predicted base-vs-fine-tuned gap. **The fix is a LoRA fine-tune**
(distil-labs recipe: base 34–63% → 96–98% on this task shape, ~1–2k synthetic
examples).

Plan for the fine-tune:
1. **Generate synthetic data** with a big model (use Groq — we have the key):
   for each template, ~50–100 phrasing variations paired with correct
   `{template, slots}` labels. Include hard negatives → `"none"`.
2. **LoRA-tune** LFM2.5-350M (unsloth or mlx-lm; ~1hr on a single modern GPU —
   the user has cloud machines, offered them).
3. **Drop the adapter into `tiny.py`** — the interface doesn't change; point
   `llama-server` at the merged/adapter GGUF.

User has offered **cloud GPUs + online services** for training/generation — use
them for step 2; Groq for step 1 (cheap).

## 6. How to run things

```sh
# tiny matcher (required for zoxide tier)
llama-server -m ~/models/LFM2.5-350M-Q4_K_M.gguf --port 8791 -ngl 99 -c 4096 --no-webui

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
