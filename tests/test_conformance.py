"""The conformance suite runner — the cross-implementation contract, executed.

tests/conformance/ is language-neutral fixture data: macro files plus a
cases.json of (input world -> expected engine decision) covering the format,
the click ladder, the resize geometry, and the verify gate. This file is the
PYTHON runner; a conforming port (AxstreamKit in Swift) ships its own runner
over the SAME data files and must produce identical decisions — including
byte-identical hashes, so verified stamps and task-family dedup interop
across engines. See tests/conformance/README.md for the contract rules.
"""

import asyncio
import json
from pathlib import Path

import pytest

from test_replay_cli import FakeDriver

from axstream import ocr
from axstream import patch as patchmod
from axstream.act import ReplayFailure, click_via_ladder
from axstream.driver import DriverComputer
from axstream.gate import (actions_hash, risky_ops, signature,
                           slot_value_hash, terminal_assert, verification,
                           verify)
from axstream.geometry import (GeometryMismatch, annotate_window_relative,
                               remap_offset, window_fraction)
from axstream.macrofile import MacroFileError, computer_for, load, parse
from axstream.ocr import TextHit
from axstream.patch import PatchHit

ROOT = Path(__file__).parent / "conformance"
DATA = json.loads((ROOT / "cases.json").read_text())
WORLDS = DATA["worlds"]
CASES = DATA["cases"]


def deep_approx(got, want, path=""):
    """Structural equality with float tolerance — fixture numbers are JSON
    decimals, engine numbers are computed doubles."""
    if isinstance(want, float) or isinstance(got, float):
        assert got == pytest.approx(want, abs=1e-6), f"{path}: {got} != {want}"
    elif isinstance(want, dict):
        assert isinstance(got, dict) and got.keys() == want.keys(), \
            f"{path}: keys {sorted(got) if isinstance(got, dict) else got} != {sorted(want)}"
        for k in want:
            deep_approx(got[k], want[k], f"{path}.{k}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), f"{path}: {got} != {want}"
        for i, (g, w) in enumerate(zip(got, want)):
            deep_approx(g, w, f"{path}[{i}]")
    else:
        assert got == want, f"{path}: {got!r} != {want!r}"


def macro_path(case) -> Path:
    return ROOT / "macros" / case["macro_file"]


# -- per-kind handlers -----------------------------------------------------


def run_parse(case, monkeypatch):
    text = "\n".join(case["macro"]) + "\n"
    expect = case["expect"]
    if "error_contains" in expect:
        with pytest.raises(MacroFileError) as ei:
            parse(text, name_hint=case.get("name_hint", ""))
        assert expect["error_contains"] in str(ei.value)
        return
    mf = parse(text, name_hint=case.get("name_hint", ""))
    assert mf.name == expect["name"]
    deep_approx(mf.actions, expect["actions"], "actions")


def run_fill(case, monkeypatch):
    mf = parse("\n".join(case["macro"]) + "\n")
    expect = case["expect"]
    if "error_contains" in expect:
        with pytest.raises(MacroFileError) as ei:
            mf.fill(case["values"])
        assert expect["error_contains"] in str(ei.value)
        return
    if "used_slots" in expect:
        assert sorted(mf.used_slots()) == expect["used_slots"]
    deep_approx(mf.fill(case["values"]), expect["actions"], "actions")


def run_window_fraction(case, monkeypatch):
    got = window_fraction(case["gx"], case["gy"], case["bounds"])
    deep_approx(got, case["expect"]["win"], "win")


def run_remap(case, monkeypatch):
    expect = case["expect"]
    live_w, live_h = case["live"]
    if expect.get("refuse"):
        with pytest.raises(GeometryMismatch) as ei:
            remap_offset(case["win"], live_w, live_h)
        assert expect["reason_contains"] in str(ei.value)
        return
    dx, dy, mode = remap_offset(case["win"], live_w, live_h)
    assert mode == expect["mode"]
    assert dx == pytest.approx(expect["dx"], abs=1e-6)
    assert dy == pytest.approx(expect["dy"], abs=1e-6)


def run_annotate(case, monkeypatch):
    got = annotate_window_relative(case["actions"], case["window"])
    deep_approx(got, case["expect"]["actions"], "actions")


def build_world(case, monkeypatch) -> FakeDriver:
    world = WORLDS[case["world"]]
    d = FakeDriver(windows=world["windows"], elements=world["elements"],
                   shot=tuple(world["shot"]))
    o = world.get("ocr", {})
    hits = {text: TextHit(x=h["x"], y=h["y"], text=text,
                          confidence=h.get("confidence", 0.9),
                          level=h.get("level", "fast"))
            for text, h in (o.get("hits") or {}).items()}
    all_hits = [TextHit(x=h["x"], y=h["y"], text=h["text"],
                        confidence=h.get("confidence", 0.9),
                        level=h.get("level", "fast"))
                for h in (o.get("all_text") or [])]
    monkeypatch.setattr(ocr, "available", lambda: bool(o.get("available")))
    monkeypatch.setattr(ocr, "find_text", lambda path, query: hits.get(query))
    # nearest_text stays REAL — its max_dist + ambiguity rules are contract
    monkeypatch.setattr(ocr, "all_text", lambda path: all_hits)
    p = world.get("patch", {})
    hit = p.get("hit")
    monkeypatch.setattr(patchmod, "available", lambda: bool(p.get("available")))
    monkeypatch.setattr(patchmod, "find_patch",
                        lambda path, frag: PatchHit(**hit) if hit else None)
    return d


def run_ladder(case, monkeypatch):
    d = build_world(case, monkeypatch)
    expect = case["expect"]
    if expect.get("refuse"):
        with pytest.raises(ReplayFailure) as ei:
            asyncio.run(click_via_ladder(d, case["op"]))
        assert expect["reason_contains"] in str(ei.value)
        return
    line = asyncio.run(click_via_ladder(d, case["op"]))
    assert line["via"] == expect["via"]
    if "geometry" in expect:
        assert line.get("geometry") == expect["geometry"]
    if "snapped_to" in expect:
        assert line.get("snapped_to") == expect["snapped_to"]
    if "note_contains" in expect:
        assert expect["note_contains"] in line.get("note", "")
    clicks = [args for name, args in d.calls if name == "click"]
    assert clicks, "no click was delivered"
    delivered = expect["delivered"]
    if "element_index" in delivered:
        assert clicks[-1]["element_index"] == delivered["element_index"]
    else:
        assert clicks[-1]["x"] == pytest.approx(delivered["x"], abs=0.51)
        assert clicks[-1]["y"] == pytest.approx(delivered["y"], abs=0.51)


def run_gate_predicates(case, monkeypatch):
    mf = load(macro_path(case))
    expect = case["expect"]
    assert terminal_assert(mf) is expect["terminal_assert"]
    assert risky_ops(mf) == expect["risky_ops"]
    assert verification(mf)["state"] == expect["verification_state"]


def run_hash(case, monkeypatch):
    mf = load(macro_path(case))
    expect = case["expect"]
    if "actions_hash" in expect:
        assert actions_hash(mf) == expect["actions_hash"]
    if "signature" in expect:
        assert signature(mf) == expect["signature"]


def run_signature_pair(case, monkeypatch):
    a, b = (load(ROOT / "macros" / f) for f in case["macro_files"])
    same = signature(a) == signature(b)
    assert same is case["expect"]["same_signature"]


def run_slot_value_hash(case, monkeypatch):
    assert slot_value_hash(case["value"]) == case["expect"]["hash"]


def run_verify_reject(case, monkeypatch):
    # every reject here fires BEFORE the live replay — no driver is touched
    result = verify(str(macro_path(case)))
    assert result["ok"] is False
    assert case["expect"]["reason_contains"] in result["reason"]


def run_executor(case, monkeypatch):
    computer = computer_for(load(macro_path(case)))
    from axstream.phone import PhoneComputer
    if case["expect"]["executor"] == "phone":
        assert isinstance(computer, PhoneComputer)
    else:
        assert type(computer) is DriverComputer


HANDLERS = {
    "parse": run_parse,
    "fill": run_fill,
    "window_fraction": run_window_fraction,
    "remap": run_remap,
    "annotate": run_annotate,
    "ladder": run_ladder,
    "gate_predicates": run_gate_predicates,
    "hash": run_hash,
    "signature_pair": run_signature_pair,
    "slot_value_hash": run_slot_value_hash,
    "verify_reject": run_verify_reject,
    "executor": run_executor,
}


def test_every_case_kind_has_a_handler():
    assert {c["kind"] for c in CASES} <= set(HANDLERS)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_conformance(case, monkeypatch):
    HANDLERS[case["kind"]](case, monkeypatch)
