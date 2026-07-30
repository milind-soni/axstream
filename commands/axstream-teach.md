---
description: Compile a UI task into a fast replayable macro (do it once, instant forever)
---

The user wants to teach axstream a repeatable macOS UI task: $ARGUMENTS

Follow the axstream skill's compile loop precisely:

1. If the task description is empty, ask what task to teach.
2. Check `axstream list --json` first — if a macro for this task already exists, say so and stop (or offer to re-verify it).
3. Perform the task ONCE using the axstream MCP `act` tool with a single assert-guarded batch (prefer key shortcuts over clicking chrome; never target text the batch itself types; end with an assert that only passes when the outcome is real). Use `screen_text`/`find` to ground targets — not screenshots.
4. If the batch succeeded, save it with `write_macro`: kebab-case name, a good `when_to_use`, slots for the values that vary (with example values), and the terminal assert.
5. If a live re-run is side-effect-safe, run `verify_macro` so it earns the verified stamp. If re-running would duplicate a side effect (sent message, created record), say why you're leaving it unverified.
6. Report: the macro name, its slots, verified state, and the one-liner to use it next time (`axstream replay <name> --slots ...`).
