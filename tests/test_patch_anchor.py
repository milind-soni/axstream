"""Visual patch anchors (patch.py): capture with the uniqueness gate, re-find
under drift / theme flips / known rescales, the patch_anchor replay rung, and
`replay --learn` write-back through macrofile.save_patches.

All images are synthetic (numpy-drawn UI), no driver or screen needed."""

import asyncio
import base64
import json

import pytest

cv2 = pytest.importorskip("cv2")
import numpy as np  # noqa: E402  (numpy arrives with opencv)

from axstream import patch as patchmod  # noqa: E402
from axstream.macrofile import load, save_patches  # noqa: E402
from axstream.replay import run_actions  # noqa: E402
from axstream.spec import validate_op  # noqa: E402

from test_replay_cli import WINDOW, FakeDriver  # noqa: E402


# -- synthetic UI ----------------------------------------------------------

W, H = 1600, 1200
BTN = (600, 400)  # top-left of the one unique button


def draw_ui(shift=(0, 0), invert=False, icon_count=1):
    """A light canvas with ONE unique button and `icon_count` identical
    lock-ish icons. Returns uint8 grayscale (H, W)."""
    img = np.full((H, W), 230, np.uint8)
    x, y = BTN[0] + shift[0], BTN[1] + shift[1]
    cv2.rectangle(img, (x, y), (x + 140, y + 56), 40, 2)
    cv2.circle(img, (x + 40, y + 28), 14, 90, -1)
    cv2.line(img, (x + 70, y + 12), (x + 126, y + 44), 60, 3)
    cv2.line(img, (x + 70, y + 44), (x + 126, y + 12), 60, 3)
    for i in range(icon_count):
        ix, iy = 200, 150 + i * 220  # spaced beyond the crop height
        cv2.rectangle(img, (ix, iy), (ix + 24, iy + 24), 70, -1)
        cv2.rectangle(img, (ix + 6, iy - 10), (ix + 18, iy), 70, 2)
    return 255 - img if invert else img


def write_png(tmp_path, img, name="shot.png"):
    p = tmp_path / name
    cv2.imwrite(str(p), img)
    return str(p)


BTN_CENTER = (BTN[0] + 70, BTN[1] + 28)


def capture(tmp_path, **kw):
    shot = write_png(tmp_path, draw_ui(**kw), "rec.png")
    return patchmod.capture_patch(shot, *BTN_CENTER)


# -- capture: the uniqueness gate ------------------------------------------

def test_capture_unique_button(tmp_path):
    frag = capture(tmp_path)
    assert frag is not None
    assert frag["sw"] == W and frag["sh"] == H
    assert abs(frag["fx"] * W - BTN_CENTER[0]) < 1
    live = write_png(tmp_path, draw_ui(), "live.png")
    hit = patchmod.find_patch(live, frag)
    assert hit is not None and hit.method == "raw"
    assert abs(hit.x - BTN_CENTER[0]) <= 2 and abs(hit.y - BTN_CENTER[1]) <= 2


def test_capture_refuses_repeated_icon(tmp_path):
    shot = write_png(tmp_path, draw_ui(icon_count=4))
    assert patchmod.capture_patch(shot, 212, 162) is None  # a lock, one of 4


def test_capture_refuses_featureless_chrome(tmp_path):
    shot = write_png(tmp_path, draw_ui())
    assert patchmod.capture_patch(shot, 1200, 900) is None  # blank canvas


def test_capture_out_of_bounds(tmp_path):
    shot = write_png(tmp_path, draw_ui())
    assert patchmod.capture_patch(shot, -5, 50) is None
    assert patchmod.capture_patch(shot, W + 1, 50) is None


# -- find: drift, themes, scale --------------------------------------------

def test_find_survives_small_drift(tmp_path):
    frag = capture(tmp_path)
    live = write_png(tmp_path, draw_ui(shift=(40, -30)), "live.png")
    hit = patchmod.find_patch(live, frag)
    assert hit is not None
    assert abs(hit.x - (BTN_CENTER[0] + 40)) <= 2
    assert abs(hit.y - (BTN_CENTER[1] - 30)) <= 2


def test_find_refuses_far_drift(tmp_path):
    # a patch is a local verifier: beyond the search window it must MISS,
    # not go hunting across the screen for something that looks right
    frag = capture(tmp_path)
    live = write_png(tmp_path, draw_ui(shift=(500, 0)), "live.png")
    assert patchmod.find_patch(live, frag) is None


def test_find_survives_theme_flip_via_edges(tmp_path):
    frag = capture(tmp_path)
    live = write_png(tmp_path, draw_ui(invert=True), "live.png")
    hit = patchmod.find_patch(live, frag)
    assert hit is not None and hit.method == "edge"
    assert abs(hit.x - BTN_CENTER[0]) <= 3 and abs(hit.y - BTN_CENTER[1]) <= 3


def test_find_rescales_for_known_size_change(tmp_path):
    frag = capture(tmp_path)
    scaled = cv2.resize(draw_ui(), (int(W * 0.9), int(H * 0.9)))
    live = write_png(tmp_path, scaled, "live.png")
    hit = patchmod.find_patch(live, frag)
    assert hit is not None
    assert abs(hit.x - BTN_CENTER[0] * 0.9) <= 4
    assert abs(hit.y - BTN_CENTER[1] * 0.9) <= 4


def test_find_missing_control(tmp_path):
    frag = capture(tmp_path)
    live = write_png(tmp_path, np.full((H, W), 230, np.uint8), "live.png")
    assert patchmod.find_patch(live, frag) is None


# -- spec ------------------------------------------------------------------

def _fragment_stub():
    return {"png": base64.b64encode(b"notapng").decode(), "fx": 0.5, "fy": 0.5,
            "sw": 1600, "sh": 1200}


def test_spec_accepts_patch_only_target():
    ok, err = validate_op({"op": "act", "do": "click",
                           "target": {"patch": _fragment_stub()}})
    assert ok, err


def test_spec_rejects_malformed_patch():
    bad = {"png": "", "fx": 0.5, "fy": 0.5, "sw": 1600, "sh": 1200}
    ok, _ = validate_op({"op": "act", "do": "click", "target": {"patch": bad}})
    assert not ok
    assert not patchmod.valid_fragment({"png": "x", "fx": "a", "fy": 0,
                                        "sw": 1, "sh": 1})


def test_find_patch_rejects_garbage_png(tmp_path):
    live = write_png(tmp_path, draw_ui(), "live.png")
    assert patchmod.find_patch(live, _fragment_stub()) is None


# -- macro-file write-back -------------------------------------------------

MACRO = """\
{"name":"demo","description":"d"}
# a comment the rewrite must keep
{"op":"act","do":"open","target":"Notes"}

{"op":"act","do":"click","target":{"x":500,"y":250}}
{"op":"done","status":"success"}
"""


def test_save_patches_preserves_everything_else(tmp_path):
    p = tmp_path / "demo.axstream"
    p.write_text(MACRO)
    frag = _fragment_stub()
    assert save_patches(p, {1: frag}) == 1  # index 1 = the click op line
    text = p.read_text()
    assert "# a comment the rewrite must keep" in text
    assert "\n\n" in text  # the blank line survived
    mf = load(p)
    assert mf.actions[1]["target"]["patch"] == frag
    assert mf.actions[0]["target"] == "Notes"  # untouched op stayed verbatim


def test_save_patches_skips_targetless_ops(tmp_path):
    p = tmp_path / "demo.axstream"
    p.write_text(MACRO)
    assert save_patches(p, {2: _fragment_stub()}) == 0  # done has no target


# -- the replay rung + --learn, against a shot-writing fake driver ---------

class ShotDriver(FakeDriver):
    """FakeDriver that actually WRITES self.image to the screenshot path the
    driver machinery asks for — the patch/OCR rungs read real pixels."""

    def __init__(self, image, **kw):
        super().__init__(shot=(image.shape[1], image.shape[0]), **kw)
        self.image = image

    async def tool(self, name, /, **args):
        if name == "get_window_state" and "screenshot_out_file" in args:
            cv2.imwrite(args["screenshot_out_file"], self.image)
        return await super().tool(name, **args)


def run(actions, computer, **kw):
    events: list[dict] = []
    code = asyncio.run(run_actions(actions, computer, emit=events.append, **kw))
    return code, events


def _click_args(d):
    return [a for n, a in d.calls if n == "click"][-1]


def test_patch_rung_clicks_at_matched_pixels(tmp_path):
    frag = capture(tmp_path)
    d = ShotDriver(draw_ui(shift=(40, 0)))
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250, "patch": frag}}
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "patch_anchor"
    args = _click_args(d)
    assert abs(args["x"] - (BTN_CENTER[0] + 40)) <= 2
    assert abs(args["y"] - BTN_CENTER[1]) <= 2
    # a patch-verified click is NOT a blind step
    assert "unverified_steps" not in events[-1]


def test_patch_miss_falls_through_to_pixels_and_stays_blind(tmp_path):
    frag = capture(tmp_path)
    d = ShotDriver(np.full((H, W), 230, np.uint8))  # control is gone
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250, "patch": frag}}
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    assert "patch anchor did not match" in events[0]["note"]
    assert events[-1]["unverified_steps"] == [0]


def test_learn_captures_and_replay_uses_it(tmp_path):
    # window 800x600 logical, shot 1600x1200 -> recorded click at logical
    # (100+350, 50+214) lands on the button center in shot pixels
    lx = WINDOW["bounds"]["x"] + BTN_CENTER[0] / 2
    ly = WINDOW["bounds"]["y"] + BTN_CENTER[1] / 2
    op = {"op": "act", "do": "click", "target": {"x": lx, "y": ly}}
    d = ShotDriver(draw_ui())
    learned: dict[int, dict] = {}
    code, events = run([op], d, learn=True, learned=learned)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    assert events[0].get("learned_patch") is True
    assert 0 in learned and patchmod.valid_fragment(learned[0])

    # write it into a macro file, reload, replay: now patch-verified
    p = tmp_path / "learned.axstream"
    p.write_text(json.dumps({"name": "learned"}) + "\n"
                 + json.dumps(op) + "\n"
                 + '{"op":"done","status":"success"}\n')
    assert save_patches(p, learned) == 1
    mf = load(p)
    d2 = ShotDriver(draw_ui())
    code, events = run(mf.actions, d2)
    assert code == 0
    assert events[0]["via"] == "patch_anchor"
    assert "unverified_steps" not in events[-1]


def test_learn_refuses_ambiguous_spot(tmp_path):
    # aim the recorded click at one of four identical icons: learn must
    # decline to save an anchor rather than save a wrong-row trap
    lx = WINDOW["bounds"]["x"] + 212 / 2
    ly = WINDOW["bounds"]["y"] + 162 / 2
    op = {"op": "act", "do": "click", "target": {"x": lx, "y": ly}}
    d = ShotDriver(draw_ui(icon_count=4))
    learned: dict[int, dict] = {}
    code, events = run([op], d, learn=True, learned=learned)
    assert code == 0
    assert learned == {}
    assert "learned_patch" not in events[0]


def test_learn_without_cv2_warns(tmp_path, capsys, monkeypatch):
    # --learn with no opencv must SAY it can't learn, not silently no-op
    # (found live: a full pip-install replay learned nothing with no hint)
    from axstream.replay import cmd_replay
    monkeypatch.setattr(patchmod, "_CV2", False)
    p = tmp_path / "m.axstream"
    p.write_text('{"op":"act","do":"wait","ms":1}\n')
    monkeypatch.setattr("axstream.replay.DriverComputer", None, raising=False)
    monkeypatch.chdir(tmp_path)
    import axstream.replay as replay_module

    class NoDriver:  # connect is never reached if we bail after the warning
        def __init__(self):
            raise RuntimeError("stop before driving")

    monkeypatch.setattr("axstream.driver.DriverComputer", NoDriver)
    try:
        cmd_replay([str(p), "--learn"])
    except RuntimeError:
        pass
    out = capsys.readouterr().out
    assert "--learn needs opencv" in out
