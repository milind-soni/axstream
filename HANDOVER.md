# axstream — Handover

Current state and the finishing plan only. The full build history (voice
tier, matcher training, benchmarks, driver archaeology) lives in git: read
`git log`, tag `v0.2.0`, and the memory of whoever sent you here.

## THE 0.3.x STATE + THE FINISHING PLAN (2026-08-09, updated)

Current truth: 0.3.1 on PyPI + origin/main = the clean core (format, ladder,
gate, driver, MCP, codex capture, phone backend), 184 tests (134 unit +
50 conformance), ~5,400 lines.
Voice/matcher/burst tier parked at tag v0.2.0 — recover with
`git checkout v0.2.0`, do not re-braid it into core.

### 1. Structure — DONE (2026-08-09 pass 2)

- replay.py split: the click ladder + dispatch live in act.py; cmd_bench in
  bench.py; replay.py keeps run_actions + replay/list CLI (322 lines).
- see.py: window-as-text perception (targeted_computer / capture /
  window_view) extracted from the MCP screen_text/find/check handlers.
- check.py merge fixed: the conditions+settle merge had shipped with missing
  imports (hashlib/Path, an `_ocr` typo) — scroll_screen/wait_stable crashed
  on call. Now covered by tests/test_check.py.
- mcp.py `act` tool fixed: it referenced a macro variable that only exists
  in the replay handler and crashed on every call. Covered by
  tests/test_mcp_tools.py.
- driver.py: app-name resolution (match_app_name) moved to ax.py. The
  DriverComputer class stays whole — it IS one concern (the cua-driver
  edge); splitting it across files adds indirection, not clarity.
- Skill duplication fixed: skills/axstream/SKILL.md is now a symlink to the
  packaged axstream/skills/axstream/SKILL.md (single source of truth).
- phone.py: ensure_ready caches a ready verdict for 2s (a burst of taps no
  longer pays a screenshot+OCR scan per step), and state() reports
  ocr_available so a blind blocked-scan is visible. tests/test_phone.py.
- phone typing FIXED + live-verified (752e925): keyboard focus via
  NSWorkspace check + app activation + measured 1.5s settle + 2s trust
  window (z-order lies; NSWorkspace polling freezes in runloop-less procs
  — OCR is the only honest verifier); _hid_key no longer latches modifiers
  (flags on down only); PhoneComputer fresh-preflight recursion fixed;
  `axstream phone` script helpers now run on PhoneComputer hands.

### 2. Usability (the 2-minute first success) — REMAINING
- Docs site: index/quickstart/agents rewritten for the 0.3.x scope; voice
  pages replaced by one "parked at v0.2.0" page. Roadmap pruned to what's
  real.
- `axstream install` grows the driver step: download official cua-driver
  installer + open the two permission panes (the last manual friction).
- Error-message audit: every failure must name its fix (mostly true; check
  doctor, gate refusals, phone connection errors).

### 3. Proof
- Conformance fixtures — DONE (2026-08-09): tests/conformance/ is the
  language-neutral contract (fixture macros + cases.json of world → expected
  decision; 50 cases over format, ladder rungs + refusals, resize geometry,
  gate predicates/rejections, pinned canonical hashes so verified stamps and
  dedup interop across engines). tests/test_conformance.py is the Python
  runner; AxstreamKit's Swift runner consumes the SAME files. Rules in
  tests/conformance/README.md.
- REMAINING: one clean-room walkthrough: fresh venv, stock driver, zero
  grants assumed; time from zero to first verified replay. Target < 3
  minutes.

Definition of done: a stranger (human or agent) reaches a verified replay in
under 3 minutes; every file is one concern; the docs describe only what
exists. Nothing else belongs in core — new capability goes in as a separate
layer or not at all.
