# The axstream conformance suite

This directory is the **cross-implementation contract**: language-neutral
fixture data that every axstream engine must satisfy. Today that is the
Python core (`tests/test_conformance.py` is its runner); the planned
AxstreamKit Swift port ships its own runner over these SAME files and must
produce identical decisions. If both engines pass, a macro authored, verified,
or deduped by one is trustworthy to the other.

## What is pinned

| kind | contract |
|---|---|
| `parse`, `fill` | the `.axstream` format: header/op split, `{"op":"click"}` shorthand normalization, comments/blanks, slot fill (declared+used ⇒ required; undeclared placeholders stay verbatim), error classes |
| `window_fraction`, `remap`, `annotate` | the resize geometry: fraction derivation, edge-anchored remap, the ±2pt size tolerance, the 1.6× refusal ratio, out-of-window refusal — exact numbers |
| `ladder` | the click resolution ladder: which rung fires in a described world (AX element → OCR anchor → patch anchor → window pixel), where the click lands in window-screenshot pixels, when the engine REFUSES instead of clicking blind, bubble-snap on anchored remaps |
| `gate_predicates`, `verify_reject` | the verify gate: terminal-assert detection, risky-op refusal, stamp states (verified/stale/unverified), pre-replay rejections (no assert / risky / slot without example / captured-slot reuse) |
| `hash`, `signature_pair`, `slot_value_hash` | **byte-identical hashing.** Pinned hex strings force every engine to reproduce the canonical JSON serialization — verified stamps and task-family dedup must interop across engines, so a serialization drift is a LOUD failure here, not a silent stamp invalidation |
| `executor` | `device:"phone"` in the header routes replay through the phone hands; everything else gets the desktop driver |

## The world-stub rule

Ladder cases describe a *world* (windows, AX elements, screenshot size, OCR
hits, patch hits). The contract is **world → decision**; each runner stubs its
own perception layer to serve that world:

- `ocr.available` / `ocr.find_text` are stubbed straight from the world's
  `ocr` table. `ocr.all_text` is stubbed, but `nearest_text` runs REAL — its
  max-dist and 1.5× ambiguity rules are part of the contract.
- `patch.available` / `patch.find_patch` are stubbed from the `patch` table.
- The window/driver layer is simulated at the driver message level
  (`list_windows` / `get_window_state` shapes) so the engine's real snapshot,
  caching, and pixel-space machinery runs. A Swift runner simulates its
  native observation layer at the equivalent seam.

Coordinates in `delivered` are **window-screenshot pixels** (top-left origin,
the fixtures use a clean 2× Retina space: 800×600 logical → 1600×1200 px).

## Changing the contract

A behavior change that fails a case here is a **compat break** between
engines — change the case in the same commit and flag it loudly. New rungs,
refusal classes, or header fields get new cases FIRST. The pinned hashes in
`cases.json` were computed by the Python engine; if canonical serialization
ever changes intentionally, recompute them and treat every stored verified
stamp as invalidated (the stamp hash will read as stale — that is by design).
