"""The fast replay ladder: element-walk-free pixel clicks (window_geometry +
size-keyed dims cache), window-relative resize-aware targets (geometry.py),
the OCR text-anchor rung, and the sharpened blind-step accounting."""

import asyncio
import json

import pytest

from axstream import ocr
from axstream.geometry import (
    GeometryMismatch,
    annotate_window_relative,
    remap_offset,
    window_fraction,
)
from axstream.ocr import TextHit
from axstream.replay import cmd_replay, run_actions
from axstream.spec import validate_op

from test_replay_cli import ELEMENTS, WINDOW, FakeDriver


def run(actions, computer):
    events: list[dict] = []
    code = asyncio.run(run_actions(actions, computer, emit=events.append))
    return code, events


def gws_calls(d):
    return [a for n, a in d.calls if n == "get_window_state"]


# -- Phase 1: pixel clicks never pay the element walk ----------------------

def test_pixel_click_skips_element_walk():
    op = {"op": "act", "do": "click", "target": {"x": 500, "y": 250}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    walks = gws_calls(d)
    assert len(walks) == 1
    assert walks[0]["max_elements"] == 1  # geometry snapshot, not a tree walk
    args = [a for n, a in d.calls if n == "click"][-1]
    assert (args["x"], args["y"]) == (800, 400)


def test_repeat_pixel_clicks_served_from_dims_cache():
    ops = [{"op": "act", "do": "click", "target": {"x": 500, "y": 250}}] * 3
    d = FakeDriver()
    code, _ = run(ops, d)
    assert code == 0
    # one geometry snapshot total; repeats cost only a list_windows each
    assert len(gws_calls(d)) == 1
    assert len([n for n, _ in d.calls if n == "list_windows"]) >= 3


def test_dims_cache_survives_window_move_but_not_resize():
    d = FakeDriver()
    op = {"op": "act", "do": "click", "target": {"x": 500, "y": 250}}
    run([op], d)
    assert len(gws_calls(d)) == 1
    # window MOVED: same size, new origin -> cache still valid
    d.windows = [{**WINDOW, "bounds": {**WINDOW["bounds"], "x": 300, "y": 90}}]
    run([{"op": "act", "do": "click", "target": {"x": 700, "y": 290}}], d)
    assert len(gws_calls(d)) == 1
    # window RESIZED -> dims (and the driver's downscale ratio) are stale
    d.windows = [{**WINDOW, "bounds": {**WINDOW["bounds"], "width": 900}}]
    run([op], d)
    assert len(gws_calls(d)) == 2


def test_labeled_click_still_walks_the_tree():
    op = {"op": "act", "do": "click", "target": {"ax": {"title": "New Note"}}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "ax_element"
    assert gws_calls(d)[0]["max_elements"] == 500


# -- Phase 2a: window-relative geometry ------------------------------------

def test_window_fraction_roundtrip():
    win = window_fraction(500, 250, {"x": 100, "y": 50, "w": 800, "h": 600})
    assert win == {"fx": 0.5, "fy": pytest.approx(1 / 3), "w": 800, "h": 600}
    # a click outside the window carries no window-relative meaning
    assert window_fraction(50, 250, {"x": 100, "y": 50, "w": 800, "h": 600}) is None


def test_remap_exact_when_size_unchanged():
    win = {"fx": 0.5, "fy": 0.25, "w": 800, "h": 600}
    assert remap_offset(win, 800, 600) == (400, 150, "exact")


def test_remap_edge_anchors_on_resize():
    # a "Done"-style control near the bottom-right keeps its edge distances
    win = {"fx": 0.95, "fy": 0.9, "w": 800, "h": 600}
    dx, dy, mode = remap_offset(win, 1000, 700)
    assert mode == "anchored"
    assert dx == pytest.approx(1000 - (800 - 760))   # 40pt from the right
    assert dy == pytest.approx(700 - (600 - 540))    # 60pt from the bottom
    # a sidebar-style control near the top-left keeps absolute offsets
    win = {"fx": 0.1, "fy": 0.05, "w": 800, "h": 600}
    dx, dy, mode = remap_offset(win, 1000, 700)
    assert (dx, dy, mode) == (80, 30, "anchored")


def test_remap_refuses_wild_resize():
    win = {"fx": 0.5, "fy": 0.5, "w": 800, "h": 600}
    with pytest.raises(GeometryMismatch, match="refusing to click blind"):
        remap_offset(win, 2000, 600)
    with pytest.raises(GeometryMismatch):
        remap_offset(win, 300, 600)


def test_annotate_from_header_window():
    actions = [
        {"op": "act", "do": "click", "target": {"x": 500, "y": 250}},
        {"op": "act", "do": "type", "text": "hi"},
        {"op": "act", "do": "click",
         "target": {"x": 1, "y": 2, "win": {"fx": 0.1, "fy": 0.1, "w": 5, "h": 5}}},
    ]
    out = annotate_window_relative(actions, {"x": 100, "y": 50, "w": 800, "h": 600})
    assert out[0]["target"]["win"]["fx"] == 0.5
    assert actions[0]["target"] == {"x": 500, "y": 250}  # input never mutated
    assert out[1] is actions[1]
    assert out[2]["target"]["win"]["w"] == 5  # recorder-emitted win kept


def test_win_target_replays_after_resize():
    # recorded on the 800x600 window; live window is 900x600 — the click is
    # near the top-left, so the anchored offset is the recorded one
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250,
                     "win": {"fx": 0.5, "fy": 1 / 3, "w": 900, "h": 600}}}
    d = FakeDriver()  # live: 800x600 at 2x -> remap 450pt -> px scale 1600/800
    code, events = run([op], d)
    assert code == 0
    assert events[0]["geometry"] == "anchored"
    args = [a for n, a in d.calls if n == "click"][-1]
    assert (args["x"], args["y"]) == (900, 400)  # 450*2, 200*2


def test_win_target_refuses_wild_resize_as_failure_json():
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250,
                     "win": {"fx": 0.5, "fy": 0.5, "w": 1600, "h": 600}}}
    code, events = run([op], FakeDriver())
    assert code == 1
    assert "refusing to click blind" in events[-1]["reason"]


def test_cli_dry_annotates_header_window(tmp_path, capsys):
    header = {"name": "resize_aware",
              "window": {"x": 100, "y": 50, "w": 800, "h": 600}}
    p = tmp_path / "resize_aware.axstream"
    p.write_text(json.dumps(header) + "\n"
                 + json.dumps({"op": "act", "do": "click",
                               "target": {"x": 500, "y": 250}}) + "\n")
    assert cmd_replay([str(p), "--dry"]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["op"]["target"]["win"] == {
        "fx": 0.5, "fy": pytest.approx(1 / 3), "w": 800, "h": 600}


# -- Phase 2b: the OCR text-anchor rung -------------------------------------

class ShotWritingDriver(FakeDriver):
    """Geometry snapshots 'write' a screenshot so the OCR rung sees a path."""

    async def tool(self, name, /, **args):
        if name == "get_window_state" and "screenshot_out_file" in args:
            with open(args["screenshot_out_file"], "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        return await super().tool(name, **args)


def test_ocr_anchor_clicks_screenshot_pixels(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_text", lambda path, q: TextHit(
        x=1130.0, y=170.0, text="New Note", confidence=0.9, level="fast"))
    # label present but NOT in the (truncated) AX tree — the Notes case
    op = {"op": "act", "do": "click", "target": {"ax": {"title": "New Note"}}}
    d = ShotWritingDriver(elements=[ELEMENTS[0]])
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "ocr_anchor"
    assert "New Note" in events[0]["resolved"]
    args = [a for n, a in d.calls if n == "click"][-1]
    # OCR hits are already in screenshot-pixel space: clicked verbatim
    assert (args["x"], args["y"]) == (1130.0, 170.0)
    # a target-verified click is NOT flagged blind
    assert "unverified_steps" not in events[-1]


def test_explicit_text_anchor_beats_ax_title(monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr, "available", lambda: True)

    def fake_find(path, q):
        seen["q"] = q
        return TextHit(x=10, y=20, text=q, confidence=0.9, level="fast")

    monkeypatch.setattr(ocr, "find_text", fake_find)
    op = {"op": "act", "do": "click",
          "target": {"text": "Save As…", "ax": {"title": "Save"}}}
    code, events = run([op], ShotWritingDriver(elements=[ELEMENTS[0]]))
    assert code == 0
    assert seen["q"] == "Save As…"


def test_ocr_miss_falls_through_to_pixels(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_text", lambda path, q: None)
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250, "ax": {"title": "Ghost"}}}
    d = ShotWritingDriver(elements=[ELEMENTS[0]])
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    assert "OCR did not find" in events[0]["note"]
    assert events[-1]["unverified_steps"] == [0]


def test_ocr_unavailable_is_soft(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250, "ax": {"title": "Ghost"}}}
    code, events = run([op], FakeDriver(elements=[ELEMENTS[0]]))
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    assert "OCR unavailable" in events[0]["note"]


def test_ocr_only_target_miss_is_honest_failure(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_text", lambda path, q: None)
    op = {"op": "act", "do": "click", "target": {"text": "Publish"}}
    code, events = run([op], ShotWritingDriver(elements=[ELEMENTS[0]]))
    assert code == 1
    assert "OCR did not find" in events[-1]["reason"]


# -- blind accounting -------------------------------------------------------

def test_ax_element_click_is_target_verified():
    op = {"op": "act", "do": "click", "target": {"ax": {"title": "New Note"}}}
    code, events = run([op], FakeDriver())
    assert code == 0
    assert events[0]["via"] == "ax_element"
    assert "unverified_steps" not in events[-1]


# -- spec: the new target shapes --------------------------------------------

def test_spec_accepts_win_and_text_targets():
    ok, _ = validate_op({"op": "act", "do": "click",
                         "target": {"win": {"fx": 0.5, "fy": 0.5, "w": 800, "h": 600}}})
    assert ok
    ok, _ = validate_op({"op": "act", "do": "click", "target": {"text": "New Note"}})
    assert ok
    ok, _ = validate_op({"op": "act", "do": "click", "target": {"text": "  "}})
    assert not ok
    ok, _ = validate_op({"op": "act", "do": "click",
                         "target": {"win": {"fx": 0.5, "fy": 0.5}}})
    assert not ok


def test_ocr_png_size_reads_ihdr(tmp_path):
    import struct
    p = tmp_path / "t.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 640, 480))
    assert ocr.png_size(str(p)) == (640, 480)
    assert ocr.png_size(str(tmp_path / "missing.png")) is None


# -- bubble-cursor snap on anchored remaps -----------------------------------

def test_nearest_text_requires_unambiguous(monkeypatch):
    hits = [TextHit(x=100, y=100, text="Save", confidence=0.9, level="fast"),
            TextHit(x=118, y=100, text="Cancel", confidence=0.9, level="fast")]
    monkeypatch.setattr(ocr, "all_text", lambda p: hits)
    # two candidates at comparable distance -> refuse to guess
    assert ocr.nearest_text("x.png", 109, 100, max_dist=50) is None
    # one clear winner -> snap
    assert ocr.nearest_text("x.png", 99, 100, max_dist=50).text == "Save"
    # nothing within radius -> no snap
    assert ocr.nearest_text("x.png", 400, 400, max_dist=50) is None


def test_anchored_remap_snaps_to_nearby_text(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    # anchored remap lands at (900, 400) px; a text line sits 10px away
    monkeypatch.setattr(ocr, "nearest_text", lambda p, x, y, max_dist: TextHit(
        x=910.0, y=404.0, text="Bold", confidence=0.9, level="fast"))
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250,
                     "win": {"fx": 0.5, "fy": 1 / 3, "w": 900, "h": 600}}}
    d = ShotWritingDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["geometry"] == "anchored"
    assert events[0]["snapped_to"] == "Bold"
    args = [a for n, a in d.calls if n == "click"][-1]
    assert (args["x"], args["y"]) == (910.0, 404.0)


def test_exact_remap_never_snaps(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    called = {}
    monkeypatch.setattr(ocr, "nearest_text",
                        lambda *a, **k: called.setdefault("hit", True))
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250,
                     "win": {"fx": 0.5, "fy": 1 / 3, "w": 800, "h": 600}}}
    code, events = run([op], ShotWritingDriver())
    assert code == 0
    assert events[0]["geometry"] == "exact"
    assert "snapped_to" not in events[0] and not called


# -- OCR outcome assertions ---------------------------------------------------

def test_assert_text_polls_ocr_and_passes(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_text", lambda p, q: TextHit(
        x=1, y=2, text=q, confidence=0.9, level="fast"))
    op = {"op": "assert", "target": {"text": "weather in delhi"}}
    code, events = run([op], ShotWritingDriver())
    assert code == 0
    assert events[0]["via"] == "ocr" and "visible" in events[0]["resolved"]


def test_assert_text_fails_honestly(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_text", lambda p, q: None)
    op = {"op": "assert", "target": {"text": "never rendered"}}
    code, events = run([op], ShotWritingDriver())
    assert code == 1
    assert "not visible" in events[-1]["reason"]
