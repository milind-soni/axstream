---
name: axstream
description: Fast deterministic macOS UI automation via replayable macros. Use BEFORE driving any macOS app's UI by hand (clicking/typing via computer-use or screenshots) — if a macro exists, replay finishes the task in seconds with no per-step reasoning; after doing a UI task manually, save it as a macro so the next run is fast. Triggers on repetitive desktop tasks, "do X in <app> again", and any macOS UI work.
---

# axstream — replay UI tasks instead of re-deriving them

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

The compile loop (this is what makes everything fast over time): novel task
-> do it with ONE `act` batch (assert-guarded) -> if it succeeded, you
already hold the exact op list -> `write_macro` it -> `verify_macro` when a
live re-run is safe -> from then on it's a single `replay_macro` call. Fall
back to native screenshot computer use only for visual judgment the
primitives can't express (canvas apps, images, layout questions).

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
   macro file (format below), `--dry` it, replay it once to verify, and save
   it to `~/.axstream/macros/<name>.axstream`. Next time it's one command.

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

Ops: `open` (app name or URL) · `click` / `double_click` · `type` · `key` ·
`scroll` (`direction`, `clicks`) · `wait` (`ms`) · `move` ·
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
