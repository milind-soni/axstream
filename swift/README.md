# axstream-bar (Swift port)

Single-process macOS port of the axstream voice-driven computer-use pipeline:
hold ⌃⌥ (Control+Option) and speak → Parakeet transcribes locally →
Groq streams JSONL actions → cua-driver executes them, all shown in a
floating bottom bar.

## Run

```sh
cd /Users/milindsoni/Documents/mywork/axstream/swift
swift build
swift run axstream-bar
```

Run it **from a terminal that already has Accessibility permission** (the
global ⌃⌥ hotkey monitor inherits the terminal's grant). The first time you
hold ⌃⌥ macOS will prompt for **Microphone** access — grant it to the
terminal. On first launch the Parakeet v3 model is downloaded (~600MB,
progress shown in the bar), then warmed; the bar says "hold ⌃⌥ and speak"
when ready.

Requirements already on this machine:

- `/Users/milindsoni/.local/bin/cua-driver` (spawned as `cua-driver mcp` at startup)
- `GROQ_API_KEY` in the environment or in `/Users/milindsoni/Documents/mywork/axstream/.env`
  (`CLAUDE_API` is accepted as an Anthropic fallback; Groq is primary).
  `AXSTREAM_MODEL` overrides the default `qwen/qwen3.6-27b`.

Usage: hold ⌃⌥, speak a command ("open Notes and write hello"), release.
If your transcript stabilizes mid-hold (same partial twice + ≥3 words) the
task starts **while you are still speaking**; releasing reconciles — if the
final transcript matches, the run continues, if it grew, the old run is
cancelled and a new one starts. Speaking a new command always cancels the
running one (latest-command-wins).

## Files

- `Package.swift` — SPM executable, macOS 14+, depends on FluidAudio
  (pinned to the same revision BlueyLite uses, known-good on this machine).
- `Sources/axstream-bar/main.swift` — app bootstrap (accessory NSApplication)
  plus `VoiceSession`, the port of `bridge.py`: push-to-talk state machine,
  600ms partial loop with stability-triggered eager execution, release-time
  reconciliation, latest-command-wins cancellation, 90s task timeout.
- `Bar.swift` — frameless non-activating NSPanel (640×64, bottom-center,
  `.screenSaver` level, all Spaces): status dot (gray idle / red listening /
  yellow thinking / green acting), transcript label (italic gray while
  partial), green action chips, right-aligned timing label.
- `Hotkey.swift` — global+local `flagsChanged` monitors; both ⌃ and ⌥ held →
  talk-start, either released → talk-stop.
- `Mic.swift` — AVAudioEngine input tap → AVAudioConverter → 16kHz mono
  Float32 accumulator with thread-safe snapshots for the partial loop.
- `Stt.swift` — FluidAudio Parakeet v3 (`AsrModels.downloadAndLoad` →
  `AsrManager`), hard-FIFO serialization of transcribe calls (the model is
  not concurrency-safe), fresh `TdtDecoderState` per utterance.
- `Llm.swift` — raw SSE streaming (no SDK) against Groq's OpenAI-compatible
  `/chat/completions`, `stream:true`, 429 retry honoring Retry-After /
  "try again in Xs|Xms" (≤3 retries); Anthropic `/v1/messages` fallback;
  `.env` loading with the `CLAUDE_API → ANTHROPIC_API_KEY` alias.
- `SpecCompiler.swift` — verbatim port of `spec.py` + `compiler.py`:
  op catalog validation, op-shorthand alias (`{"op":"click"}` →
  `{"op":"act","do":"click"}`), newline-committed fence compiler (any
  ` ``` ` opens, bare ` ``` ` closes, prose outside = narration, invalid
  JSON dropped, **no dedup** of identical lines).
- `Driver.swift` — MCP JSON-RPC 2.0 client over `cua-driver mcp` stdio.
  Framing verified empirically: **newline-delimited JSON** (one message per
  line, no Content-Length headers). initialize (protocolVersion 2024-11-05)
  → notifications/initialized → tools/call, with a 120s per-request watchdog.
- `Executor.swift` — eyes + hands. Observation: `get_accessibility_tree`
  (apps + z-ordered windows; frontmost = first window owned by a regular
  app) then `get_window_state {pid, window_id, include_screenshot:false}`,
  filtered to interactable roles, ids `e0…` mapped to
  `(pid, window_id, element_index)`. Execution: click/double_click via
  element_index (AX path), type → `type_text`, key → `press_key`/`hotkey`,
  scroll → `scroll`, open → `launch_app` + `bring_to_front`, move →
  `move_cursor`, wait → sleep; late-binding refresh-and-retry for
  role/title targets (ids are never re-resolved after refresh, as in
  `executor.py`).
- `Runner.swift` — the SYSTEM prompt (verbatim from `prompt.py`), user
  prompt builder, and the burst loop (max 6): observe → stream → execute
  pipelined (a producer task feeds the compiler while ops execute), stop on
  observe/done/abort.

## Deviations from the Python reference

- **Raw-coordinate clicks use desktop scope.** cua-driver's `x,y` click form
  is window-local screenshot pixels; the spec's `{"x","y"}` targets are
  screen coordinates, so bare-coordinate clicks are sent with
  `scope:"desktop"` (true screen pixels). Since the observation contains no
  coordinates, the model uses ax ids virtually always; coordinates remain
  the documented "last resort".
- **`move` maps to the agent-cursor overlay.** cua-driver's `move_cursor`
  moves its overlay cursor, not the real pointer — there is no real-pointer
  move tool. For ax targets the element's AX frame center (screen points) is
  used.
- **`open` on a URL launches Safari with the URL** (`launch_app` has no
  default-browser notion). Plain app names launch by name in the background,
  then `bring_to_front` — an "open X" voice command implies the user wants
  to see it.
- **`key`/`type`/`scroll` target the frontmost pid from the last
  observation** (cua-driver requires a pid; the Python computer-server
  posted globally).
- **Observation covers the frontmost window only** plus the app list and
  other visible window names — `get_window_state` is per-window and walking
  every window would blow the prompt budget; matches the spec's
  "frontmost app's main window" instruction.
- **stt timing is shown in the bar** instead of the Python runner's stdout
  timeline; narration/invalid-line events are received but not rendered
  (kept the bar drawing simple).
- **Risky ops are allowed and logged** (same default as the Python bridge);
  there is no interactive confirmation UI.

## Verified against the real driver

Tool schemas were pulled with `cua-driver describe <tool>` (not guessed):
`click`/`double_click`/`right_click` take `pid, window_id, element_index`
or pixel `x,y`; `type_text {pid, text}`; `press_key {pid, key, modifiers}`;
`hotkey {pid, keys}` (modifiers first, one non-modifier last — the spec's
`["cmd","s"]` shape passes through unchanged); `scroll {pid, direction,
amount ≤50}`; `launch_app {name|bundle_id, urls}` returns the pid used for
`bring_to_front {pid}`. The MCP handshake was tested by piping a handshake
into `cua-driver mcp`: responses are newline-delimited JSON.
