---
description: Benchmark a macro and show the speed story (p50/p95 per op)
---

Benchmark an axstream macro: $ARGUMENTS

1. If no macro name given, run `axstream list --json` and pick the most-used-looking verified macro (or ask).
2. Run `axstream bench <name> --warmup 1 --runs 3` (only if the macro is side-effect-safe — refuse to bench anything that sends/pays/deletes, and say why).
3. Present a compact table: per-op p50/p95 and the total, then one comparison line: what this task would roughly cost via screenshot-driven computer use (~5-15s per step) vs the measured replay total.
