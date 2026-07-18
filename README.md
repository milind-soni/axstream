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
a learned command replays in **~100ms with no LLM** at **93% end-to-end
accuracy** (fine-tuned 350M matcher, held-out eval).

## Three speeds, one language

- **Instant** — commands you've used before replay directly (frecency-ranked,
  slot-parameterized, guarded against the live screen). No LLM.
- **Fast** — novel commands streamed by an LLM over the AX tree. Every success
  is captured into the instant tier.
- **Fallback** — AX-dead apps via computer use.

## Run it

```sh
# dry demo — no keys, no server; proves the streaming overlap
uv run --with pytest python demo_dry.py

# live streaming (macOS) — needs cua's computer-server + an LLM key
cd ../cua/libs/python/computer-server && uv run python -m computer_server --port 8765
export GROQ_API_KEY=...
uv run python demo_live.py --task "open TextEdit and type hello world" --uri ws://localhost:8765/ws

# learn-and-replay (the instant tier) — also needs a tiny matcher on :8791
llama-server -m <matcher>.gguf --port 8791 -ngl 99 -c 4096 --no-webui
uv run python demo_learn.py

# tests
uv run pytest
```

Use a **fine-tuned** matcher for the instant tier (base LFM2.5-350M ≈47% e2e
vs ≈93% tuned; misses fall back to the LLM tier). `AXSTREAM_TINY_URL`
overrides the matcher endpoint.

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

## Layout

```
SPEC.md              the canonical action language (CC BY 4.0)
axstream/
  compiler.py        newline-committed stream compiler
  executor.py        pipelined executor + zoxide-tier replay
  ax.py              AX-tree observation, terse summaries, fuzzy resolve
  computer.py        computer-server WebSocket client (+ MockComputer)
  driver.py          cua-driver backend (background, pid-addressed delivery)
  macros.py          frecency-ranked parameterized macro store
  tiny.py            local tiny-model matcher (schema-constrained)
  capture.py         parameterize a successful run into a macro
  llm.py / prompt.py / runner.py / spec.py
demo_*.py            dry / live / learn / replay demos
docs/                axstream.dev (Fumadocs)
```

## License

Reference implementation: MIT ([LICENSE](LICENSE)). Spec: CC BY 4.0.
