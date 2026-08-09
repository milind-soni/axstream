---
name: axstream
description: Fast deterministic macOS UI automation via replayable macros. Use BEFORE driving any macOS app's UI by hand (clicking/typing via computer-use or screenshots) — if a macro exists, replay finishes the task in seconds with no per-step reasoning; after doing a UI task manually, save it as a macro so the next run is fast. Triggers on repetitive desktop tasks, "do X in <app> again", and any macOS UI work.
---

# axstream — replay UI tasks instead of re-deriving them

## First decision: delegate multi-step UI work to the computer-use subagent

If your harness has a Task/Agent tool and an agent named `computer-use`
(plugin form: `axstream:computer-use`) is available, DELEGATE any multi-step
UI task to it instead of driving the UI from the main conversation. It runs
on a fast model with exactly the tools below, replays a matching macro when
one exists, drives novel tasks as batched verified actions, saves a macro on
success, and returns a compact outcome report — so screenshots and OCR dumps
never enter your context, and your own turns stay fast for the rest of the
session. It can run in the background while you continue other work.

- Delegate: "open X and do Y then Z", click-through flows, reproduce-a-bug
  in a running app, verify-something-on-screen, anything worth a macro.
- Do it yourself (tools below): a single action, or when you must reason
  about visual state mid-flow.
- Authorize explicitly: if the task includes an irreversible step (send,
  pay, delete), name that exact step in the delegation prompt — the agent
  refuses `risk:"risky"` ops it wasn't explicitly given.

## Fast primitives (axstream MCP tools, if connected)

When the `axstream` MCP server is available, prefer its primitives over
screenshot-driven computer use for mechanical steps — they run on-device in
milliseconds and return text, not images:

- `screen_text` (~200ms) — the window as text lines with coordinates.
  Use INSTEAD of a screenshot to learn what's on screen.
- `find` (~30-600ms) — locate one control/text; returns a click-ready target.
- `act` — a whole batch of actions in ONE call (open/click/type/key +
  `assert` guards). Plan the sequence once; don't take a model turn per step.
- `check` (~250ms) — verify an outcome ("results page shows my query")
  without a verification screenshot.
- `begin_capture` + `compile_capture` — wrap Codex's initialized native `sky`
  computer-use object, record successful actions plus their preceding AX
  state, then compile that real trace into a parameterized macro. This is the
  native fallback bridge; cua-driver's recorder cannot observe `sky` calls.

The compile loop (this is what makes everything fast over time): novel task
-> use ONE `act` batch when the steps are clear, OR call `begin_capture`
before a native Codex computer-use fallback -> if it succeeded,
`write_macro` the act ops or `compile_capture` the native trace ->
`verify_macro` when a live re-run is safe -> from then on it's a single
`replay_macro` call. Route smartly (benchmarked, 2026-07-30):

- VISUAL-STATE questions (which item is selected/highlighted, colors,
  rendered appearance) -> go native DIRECTLY. OCR text cannot see selection
  rings; paying screen_text first and falling back doubles the cost.
- Pure READING in AX-rich apps (Safari, system apps): if your native
  computer use already returns accessibility TEXT (not screenshots), it may
  be as fast as screen_text — use whichever you have; screen_text's edge is
  apps with poor/truncated AX (Electron, canvas, custom toolbars) and hosts
  whose computer use is screenshot-based.
- axstream wins BIGGEST on: repeat tasks (replay a verified macro — measured
  2.7x faster than warm native CU on the same task), batched mutations with
  outcome asserts, and anything you'll ever do twice.

The verify gate (never trust an unchecked macro): a macro can replay to
100% and still not do the task — so every macro should END with an assert
that only passes when the outcome is real, and `verify_macro` /
`axstream verify <name>` replays it once live to earn a `verified` stamp
(shown in `list_macros`; editing the macro makes it stale). Do NOT verify
macros whose re-run has irreversible effects (sending, paying, deleting) —
the gate refuses `risk:"risky"` ops for exactly that reason. Prefer
verified macros; treat unverified ones as drafts to dry-run first.

axstream turns a macOS UI task into a text file of actions (`.axstream`) that
replays deterministically through the cua-driver daemon: no screenshots to
reason over, no per-step model calls, pid-addressed background clicks that
never steal the user's mouse. A replayed action costs ~0.2–1.3s instead of a
full perceive→think→act loop. The more tasks you save, the faster you get.

## The loop to follow

1. **Before any macOS UI task**: `axstream list --json` — one JSON object per
   macro with `name`, `description`, `when_to_use`, `slots`. If one matches,
   replay it instead of driving the UI yourself.
2. **Verify first**: `axstream replay <name> --dry` prints the resolved
   action list without executing (missing slots self-fill from header
   examples). Sanity-check the ops match the intent.
3. **Run**: `axstream replay <name> --slots '{"slot": "value"}'`
4. **After doing a UI task manually** (the slow way): write the steps as a
   macro file (format below), or compile the native trace as described next;
   `--dry` it, replay it once to verify, and save it to
   `~/.axstream/macros/<name>.axstream`. Next time it's one command.

Saving rules — macros are also launched by VOICE from the menu bar, and the
library is shared, so:

- Add `"examples"` to the header: 2–4 short utterances phrased the way a
  person would SAY the task out loud (lowercase, no punctuation), each
  slotted example containing its slot values verbatim, e.g.
  `{"utterance": "google mumbai weather", "slots": {"query": "mumbai weather"}}`.
  The voice matcher effectively never picks a template with no examples.
- Save under a NEW distinct name; NEVER delete, rename, or overwrite another
  macro file — even one that looks similar (a sphere macro is not a cylinder
  macro). True same-task twins are deduplicated automatically by
  `write_macro`'s signature upsert, which archives rather than deletes.
  Deleting a neighbor macro silently breaks the user's voice commands.

Deriving a macro from an existing flow (the preferred construction): when a
requested task differs from an existing macro only in a literal (a typed
search term, a URL, a filename), do NOT clone the macro per variant —
generalize the existing one by promoting that literal to a `{slot}` with a
`description` + `example`, add example utterances covering the variants, and
re-verify. One `blender-add-mesh` with a `{shape}` slot replaces
sphere/cube/cylinder clones and handles shapes nobody recorded. A macro is
NOT done until `verify_macro` passes: the live gate is what catches
background-input apps (it auto-learns `"delivery": "foreground"`), wrong-key
assumptions, and drifted anchors. Never write an assert that would pass even
if the task failed (asserting always-visible chrome proves nothing).

## Capturing a Codex native computer-use fallback

Use this when native computer use is the right first-run executor but the
workflow is worth repeating:

1. Call `begin_capture` with a short name, description, and `when_to_use`.
2. Initialize native computer use normally. Run the exact `node_setup` snippet
   returned by `begin_capture`; it replaces `globalThis.sky` with a thin
   recording facade while delegating every call to the original native object.
3. Drive the task normally. Call `get_app_state` immediately before any
   `element_index` action so the trace contains the durable role/title that
   index represented, and before coordinate clicks/drags so it records the
   source screenshot dimensions needed for Retina/resize-aware replay. Only
   successful actions are compiled.
4. After confirming the real outcome, call `compile_capture` with the
   `capture_id`. Parameterized values use
   `{"slot": {"value": "first-run value", "description": "...",
   "example": "..."}}`. Include a `terminal_assert` target.
5. Run the `node_teardown` snippet returned by `begin_capture` so later native
   actions stop appending to this trace. Captures are inspectable JSONL under
   `~/.axstream/captures/` and may contain UI text seen during the workflow.
6. Dry-run the macro, then `verify_macro` with a DIFFERENT slot value when a
   live rerun is safe. A different value prevents stale first-run UI from
   falsely satisfying the verification gate.

The compiler refuses native actions it cannot translate faithfully instead
of saving a partial macro. Keep those steps in native computer use until an
equivalent Axstream op exists.

Prerequisite: `axstream --doctor` must show `[ok] cua-driver`. If not, tell
the user the cua-driver daemon isn't running rather than falling back
silently.

Sandbox note (Codex and other sandboxed agents): replay connects to the
cua-driver daemon over a unix socket in `~/Library/Caches` — a sandboxed
shell can read macro files but CANNOT reach that socket, and the failure
looks exactly like "daemon not running" when the daemon is fine. Driving
the UI inherently needs access outside the workspace: request escalated /
approved execution for `axstream replay`, and only diagnose the daemon as
down if `axstream --doctor` fails OUTSIDE the sandbox too.

## Reading the output

Replay speaks JSONL — one object per action, then a summary.

- Progress lines carry `via`: `ax_element` (clicked a live accessibility
  element — target verified), `ocr_anchor` (found the label's rendered text
  on screen and clicked it — target verified), `window_pixel` (recorded
  coordinates — blind).
- Exit 0 + `{"ok": true, ...}`: done. If the summary carries
  `unverified_steps`, some clicks were blind — confirm the end state
  (screenshot or a quick check) before reporting success.
- Exit 1 + `{"failed_at": N, "op": {...}, "reason": "...", "completed": M}`:
  the macro is your handoff — take over the task at exactly op N (do that
  one step yourself), and consider fixing the macro afterwards.
- Exit 2 + `{"error": ...}`: usage/file/slot problem — fix the invocation or
  the file.

A refusal like `window width changed 800 -> 1400 ... refusing to click
blind` means the app window no longer matches the recording. Do the step
manually, then update the macro (better target, or re-record).

## Macro file format (`.axstream`)

Line 1: JSON header. Then one op per line. `#` comments and blank lines ok.

```
{"name": "notes-new-note", "description": "Create a note and type text", "when_to_use": "user wants a quick Apple Notes note", "slots": {"body": {"description": "note text", "example": "hello"}}, "window": {"x": 37, "y": 65, "w": 1396, "h": 806}}
{"op": "act", "do": "open", "target": "Notes"}
{"op": "act", "do": "key", "keys": ["cmd", "n"]}
{"op": "act", "do": "type", "text": "{body}"}
{"op": "done", "status": "success"}
```

Ops: `open` (app name or URL) · `click` / `double_click` / `right_click` · `drag` · `type` · `key` ·
`scroll` (`direction`, `clicks`) · `wait` (`ms`) · `wait_until` (`target`,
optional `timeout_ms` / `poll_ms`) · `move` ·
`assert` (precondition: fail fast if a target is missing) · `done`.

Click targets — richest wins, always include fallbacks when you have them:

- `{"ax": {"title": "Save", "role": "AXButton"}}` — accessibility label,
  fuzzy-matched live. Best.
- `{"text": "New Note"}` — OCR anchor: the label's rendered on-screen text.
  Use when the AX tree can't see the element (icon toolbars often can't be
  OCR'd either — text labels only).
- `{"x": 420, "y": 312}` — global screen points from the recording. Blind;
  survives window moves but not resizes…
- …unless the header carries `"window": {x, y, w, h}` (the window bounds at
  recording time) — then every coordinate click is remapped edge-anchored
  into the live window and survives resizes too. Always record this when
  authoring from a live window. Per-op
  `"win": {"fx": 0.3, "fy": 0.1, "w": 1396, "h": 806}` does the same thing.

Authoring rules that keep macros reliable:

- Prefer `key` shortcuts over clicking chrome (`cmd+n` beats clicking a
  toolbar "+"). Keyboard ops replay perfectly every time.
- Prefer `wait_until` over a fixed `wait` whenever visible text or an AX target
  signals readiness. It observes immediately and waits only as long as needed.
- Never target text the macro itself types (it changes every run).
- Put an `assert` before risky sequences so a changed UI fails fast.
- Slots: `{slot_name}` inside any string; declare each in the header with a
  `description` and an `example` (examples make `--dry` self-verifying).
- Search order: `./.axstream/macros/` (project) then `~/.axstream/macros/`.

## Fixing a macro after a failure

The failure JSON names the op. Typical fixes, in order of preference:
replace a chrome click with a `key` shortcut → add/repair the `ax` title →
add a `"text"` OCR anchor → add the header `"window"` bounds so coordinates
remap. Re-verify with `--dry`, then one live replay, before trusting it.
