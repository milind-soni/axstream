# axstream

**A streaming action language for computer-use agents.** An LLM streams
actions one JSON object per line; the executor performs each action the moment
its newline arrives — while the model is still generating. The newline is the
commit signal, so a half-generated action can never fire, and execution
overlaps generation instead of waiting for the full response.

**→ [Read the spec: SPEC.md](SPEC.md)** (axstream-spec 0.1)

```spec
{"op":"act","do":"open","target":"Notes"}
{"op":"act","do":"wait","ms":500}
{"op":"act","do":"type","text":"remember to buy milk"}
{"op":"done","status":"success"}
```

This repo holds the spec plus a reference implementation: the newline-committed
stream compiler ([compiler.py](axstream/compiler.py)), the action catalog
([spec.py](axstream/spec.py)), an accessibility-tree-first observer, and a
pipelined executor that drives [cua](https://github.com/trycua/cua)'s
`computer-server`.

## Why

Today's computer-use loops wait for the full model response, act once, sleep,
screenshot, and re-prompt — every step pays full decode + screenshot prefill.
axstream overlaps execution with decode and only re-observes at explicit
`observe` barriers, so a burst of N actions costs ~max(decode, execution)
instead of N × (decode + observe).

## Run the dry demo (no keys, no server)

```sh
uv run --with pytest python demo_dry.py
```

Prints a timeline showing actions executing mid-stream and the streamed-vs-
buffered comparison.

## Run live (macOS)

1. Start cua's computer-server on the host (needs Accessibility + Screen
   Recording permissions for the terminal):

   ```sh
   cd ../cua/libs/python/computer-server
   uv run python -m computer_server   # ws://localhost:8000/ws
   ```

2. In another terminal:

   ```sh
   export ANTHROPIC_API_KEY=...
   uv run python demo_live.py --task "open TextEdit and type hello world"
   ```

## Spec v0

See [axstream/spec.py](axstream/spec.py) for the op catalog and
[axstream/prompt.py](axstream/prompt.py) for the exact contract given to the
model. Key properties:

- **Line = commit unit.** A half-generated action can never execute
  (truncation-safe by construction).
- **No dedup.** Unlike json-render's idempotent patches, identical action
  lines are both executed — repetition is meaningful.
- **Late binding.** `{"ax":{"role":"AXButton","title":"Save"}}` resolves
  against the live tree right before the click, so plans survive small screen
  changes; `assert` + `observe` bound the speculation horizon.
- **Risk classes.** `"risk":"risky"` marks hard-to-undo actions; policy can
  block or gate them (`--no-risky`).

## Tests

```sh
uv run pytest
```

## License

Reference implementation: MIT (see [LICENSE](LICENSE)).
The spec ([SPEC.md](SPEC.md)) is CC BY 4.0.
