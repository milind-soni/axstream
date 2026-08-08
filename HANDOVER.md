# axstream — Handover

Current state and the finishing plan only. The full build history (voice
tier, matcher training, benchmarks, driver archaeology) lives in git: read
`git log`, tag `v0.2.0`, and the memory of whoever sent you here.

## THE 0.3.0 STATE + THE FINISHING PLAN (2026-08-09)

Everything above §this describes history; much of it references modules that
no longer exist. Current truth: 0.3.0 on PyPI + origin/main = the clean core
(format, ladder, gate, driver, MCP, codex capture, phone backend), 119 tests,
4,964 lines. Voice/matcher/burst tier parked at tag v0.2.0 — recover with
`git checkout v0.2.0`, do not re-braid it into core.

Remaining to reach "clean, simple, really usable" — three bounded passes:

### 1. Structure (pure moves, no behavior change, tests stay green)
- Split replay.py (~690): click ladder + dispatch -> act.py; replay.py keeps
  run_actions + cmd_replay/list/bench (~250).
- see.py: OCR + screen_text/find extraction (from ocr.py + mcp handlers).
- check.py: merge conditions.py + settle.py (asserts, wait_until, stability).
- driver.py (~750): keep socket + input surface; move app resolution and the
  dims cache helpers out or inline-document them. Target: every file one
  concern, <400 lines.
- mcp.py (~700): move TOOLS schemas to a tools.py data module; handlers call
  see/act/check directly.

### 2. Usability (the 2-minute first success)
- README top = four commands: install -> doctor -> teach (act batch via
  agent or write a 5-line macro) -> replay. Everything else moves below.
- Docs site: index/quickstart/agents rewritten for the 0.3.0 scope; voice
  pages replaced by one "parked at v0.2.0" page. Roadmap pruned to what's
  real.
- `axstream install` grows the driver step: download official cua-driver
  installer + open the two permission panes (the last manual friction).
- Error-message audit: every failure must name its fix (mostly true; check
  doctor, gate refusals, phone connection errors).

### 3. Proof
- Conformance fixtures: shared macros + expected ladder decisions (which
  rung, when geometry refuses, when the gate rejects). This doubles as the
  AxstreamKit (Swift port) contract.
- One clean-room walkthrough: fresh venv, stock driver, zero grants assumed;
  time from zero to first verified replay. Target < 3 minutes.

Definition of done: a stranger (human or agent) reaches a verified replay in
under 3 minutes; every file is one concern under ~400 lines; the docs
describe only what exists. Nothing else belongs in core — new capability
goes in as a separate layer or not at all.
