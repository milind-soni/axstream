"""`axstream replay`: --dry, structured progress, the failure handoff JSON,
and the click/double_click resolution ladder (AX element -> window-local
pixel -> failure)."""

import asyncio
import json

from axstream.computer import MockComputer
from axstream.driver import DriverComputer, _usable_window, window_pixels_from_screen
from axstream.macrofile import parse
from axstream.replay import cmd_list, cmd_replay, run_actions

FIXTURE = "\n".join([
    json.dumps({"name": "note", "description": "type a note",
                "slots": {"title": {"description": "text to type"}}}),
    json.dumps({"op": "act", "do": "type", "text": "{title}"}),
    json.dumps({"op": "act", "do": "wait", "ms": 1}),
]) + "\n"

# a driver-shaped window: 800x600 logical points at (100, 50), whose window
# screenshot is 1600x1200 -> a clean 2x "Retina" pixel space
WINDOW = {"window_id": 7, "pid": 42, "app_name": "Notes", "title": "Notes",
          "is_on_screen": True, "z_index": 5,
          "bounds": {"x": 100, "y": 50, "width": 800, "height": 600}}
ELEMENTS = [
    {"element_index": 0, "role": "AXWindow", "label": "Notes"},
    {"element_index": 12, "role": "AXButton", "label": "New Note",
     "frame": {"x": 550, "y": 130, "w": 30, "h": 20}},
]


class FakeDriver(DriverComputer):
    """Driver-shaped mock: logs every tool call, serves canned list_windows /
    get_window_state responses. Exercises the REAL window_snapshot /
    click_element / click_window_pixel machinery against fixtures."""

    def __init__(self, windows=None, elements=None, shot=(1600, 1200)):
        super().__init__()
        self.windows = [WINDOW] if windows is None else windows
        self.elements = ELEMENTS if elements is None else elements
        self.shot = shot
        self.calls: list[tuple[str, dict]] = []

    async def connect(self):  # pragma: no cover - nothing to connect
        pass

    async def tool(self, name, /, **args):
        self.calls.append((name, args))
        if name == "list_windows":
            return {"windows": self.windows}
        if name == "get_window_state":
            out = {"elements": self.elements}
            if "screenshot_out_file" in args:
                out["screenshot_width"], out["screenshot_height"] = self.shot
            return out
        if name == "click" and "element_index" in args:
            return {"path": "ax", "verified": False, "effect": "unverifiable"}
        if name == "double_click" and "element_index" in args:
            return {"text": "AXOpen performed on element [12]."}
        if name == "right_click" and "element_index" in args:
            return {"path": "ax", "verified": False, "effect": "unverifiable"}
        return {"path": "cgevent", "verified": False, "effect": "unverifiable"}


def test_front_window_keeps_new_substantial_untitled_document():
    old = {**WINDOW, "window_id": 6, "title": "Old document", "z_index": 5}
    new = {**WINDOW, "window_id": 7, "title": "", "z_index": 6,
           "bounds": {"x": 10, "y": 20, "width": 500, "height": 500}}
    strip = {**WINDOW, "window_id": 8, "title": "", "z_index": 10,
             "bounds": {"x": 0, "y": 0, "width": 1512, "height": 33}}
    driver = FakeDriver(windows=[strip, new, old])
    picked = asyncio.run(driver._front_window(42))
    assert picked["window_id"] == 7
    assert _usable_window(new)
    assert not _usable_window(strip)


def test_front_window_rejects_titled_context_menu_remnant():
    document = {**WINDOW, "window_id": 6, "title": "Untitled 4", "z_index": 5}
    remnant = {**WINDOW, "window_id": 7, "title": "Window", "z_index": 9,
               "bounds": {"x": 158, "y": 79, "width": 66, "height": 20}}
    driver = FakeDriver(windows=[remnant, document])
    assert not _usable_window(remnant)
    assert asyncio.run(driver._front_window(42))["window_id"] == 6


def test_front_window_prefers_title_over_offscreen_blank_placeholder():
    titled = {**WINDOW, "window_id": 6, "title": "Real document",
              "is_on_screen": False, "z_index": 5}
    placeholder = {**WINDOW, "window_id": 7, "title": "",
                   "is_on_screen": False, "z_index": 9,
                   "bounds": {"x": 0, "y": 482, "width": 500, "height": 500}}
    driver = FakeDriver(windows=[placeholder, titled])
    assert asyncio.run(driver._front_window(42))["window_id"] == 6


def run(actions, computer):
    events: list[dict] = []
    code = asyncio.run(run_actions(actions, computer, emit=events.append))
    return code, events


# -- CLI --dry (no execution dependencies) --------------------------------

def test_cli_dry_on_fixture_draft(tmp_path, capsys):
    p = tmp_path / "note.axstream"
    p.write_text(FIXTURE)
    code = cmd_replay([str(p), "--slots", '{"title": "hello"}', "--dry"])
    assert code == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0] == {"i": 0, "op": {"op": "act", "do": "type", "text": "hello"}, "dry": True}
    assert lines[-1]["dry"] is True and lines[-1]["ok"] is True
    assert lines[-1]["actions"] == 2


def test_cli_dry_missing_slot_fills_example(tmp_path, capsys):
    # A dry run executes nothing, so missing slots self-fill (header example,
    # else <name>) — a slotted macro is always verifiable without inputs.
    p = tmp_path / "note.axstream"
    p.write_text(FIXTURE)
    code = cmd_replay([str(p), "--dry"])
    assert code == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["op"] == {"op": "act", "do": "type", "text": "<title>"}
    assert lines[-1]["ok"] is True and lines[-1]["example_slots"] == ["title"]


def test_cli_real_run_missing_slot_is_usage_error(tmp_path, capsys):
    # Only --dry self-fills; a real run must still demand explicit values.
    p = tmp_path / "note.axstream"
    p.write_text(FIXTURE)
    code = cmd_replay([str(p)])
    assert code == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "title" in out["error"]


def test_cli_unknown_macro_name(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./.axstream/macros here
    code = cmd_replay(["does_not_exist", "--dry"])
    assert code == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "does_not_exist" in out["error"]
    assert out["searched"]


def test_cli_resolves_project_macro_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".axstream" / "macros"
    d.mkdir(parents=True)
    (d / "note.axstream").write_text(FIXTURE)
    code = cmd_replay(["note", "--slots", '{"title": "x"}', "--dry"])
    assert code == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[-1]["macro"] == "note"


def test_cli_list(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # isolate from the real ~/.axstream/macros (the user dir has live macros);
    # a HOME distinct from cwd, or the project dir would be listed twice
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = tmp_path / ".axstream" / "macros"
    d.mkdir(parents=True)
    (d / "note.axstream").write_text(FIXTURE)
    assert cmd_list(["--json"]) == 0
    row = json.loads(capsys.readouterr().out.strip())
    assert row["name"] == "note"
    assert row["slots"] == ["title"]
    assert row["slot_specs"]["title"]["description"] == "text to type"
    assert row["verified"] == "unverified"
    assert row["actions"] == 2


# -- execution semantics (MockComputer) -----------------------------------

def test_run_success_emits_per_action_ok():
    mf = parse(FIXTURE)
    code, events = run(mf.fill({"title": "hi"}), MockComputer(latency=0))
    assert code == 0
    assert [e["ok"] for e in events[:2]] == [True, True]
    assert events[0]["i"] == 0 and events[0]["op"]["text"] == "hi"
    assert events[-1] == {"ok": True, "completed": 2, "total": 2}


def test_failure_json_shape_on_assert():
    actions = [
        {"op": "act", "do": "wait", "ms": 1},
        {"op": "assert", "target": {"ax": {"role": "AXButton", "title": "Missing"}}},
        {"op": "act", "do": "type", "text": "never typed"},
    ]
    code, events = run(actions, MockComputer(latency=0))  # empty AX fixture
    assert code == 1
    final = events[-1]
    assert set(final) == {"failed_at", "op", "reason", "completed"}
    assert final["failed_at"] == 1
    assert final["completed"] == 1
    assert final["op"]["op"] == "assert"
    assert "did not resolve" in final["reason"]
    # the failed action also got a per-action line
    assert events[-2]["ok"] is False and events[-2]["i"] == 1


# -- the click resolution ladder (FakeDriver) ------------------------------

def test_click_ladder_prefers_ax_element():
    op = {"op": "act", "do": "click",
          "target": {"x": 563.25, "y": 138.34, "ax": {"title": "New Note"}}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "ax_element"
    assert "New Note" in events[0]["resolved"]
    assert events[0]["driver_path"] == "ax"  # the driver echoed the AX path
    clicks = [args for name, args in d.calls if name == "click"]
    assert len(clicks) == 1
    assert clicks[0].items() >= {"pid": 42, "window_id": 7, "element_index": 12}.items()
    # macro replay must NEVER issue a desktop-scope click
    assert all(args.get("scope") != "desktop" for _, args in d.calls)


def test_click_ladder_resnapshots_before_every_click():
    # the driver's element_index cache is per-snapshot (click.rs contract)
    op = {"op": "act", "do": "click", "target": {"ax": {"title": "New Note"}}}
    d = FakeDriver()
    code, _ = run([op, dict(op)], d)
    assert code == 0
    assert [n for n, _ in d.calls if n == "get_window_state"] == ["get_window_state"] * 2


def test_click_pixel_fallback_is_window_local():
    # no AX match -> recorded GLOBAL screen coords convert into window-local
    # screenshot pixels: ((500-100)*2, (250-50)*2) for the 2x window at (100,50)
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250, "ax": {"title": "No Such Label"}}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    args = [a for n, a in d.calls if n == "click"][-1]
    assert args["pid"] == 42 and args["window_id"] == 7
    assert (args["x"], args["y"]) == (800, 400)
    assert "element_index" not in args and args.get("scope") != "desktop"
    # the fallback re-snapshotted WITH a screenshot to size the pixel space
    assert any("screenshot_out_file" in a for n, a in d.calls if n == "get_window_state")


def test_pure_coordinate_click_stays_pid_addressed():
    op = {"op": "act", "do": "click", "target": {"x": 100, "y": 50}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    args = [a for n, a in d.calls if n == "click"][-1]
    assert (args["x"], args["y"]) == (0, 0)  # the window's own origin
    assert args.get("scope") != "desktop"


def test_double_click_ladder_uses_element_path():
    op = {"op": "act", "do": "double_click", "target": {"ax": {"title": "New Note"}}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "ax_element"
    args = [a for n, a in d.calls if n == "double_click"][-1]
    assert args.items() >= {"pid": 42, "window_id": 7, "element_index": 12}.items()


def test_double_click_pixel_fallback_window_local():
    op = {"op": "act", "do": "double_click", "target": {"x": 500, "y": 250}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    args = [a for n, a in d.calls if n == "double_click"][-1]
    assert args["pid"] == 42 and args["window_id"] == 7
    assert (args["x"], args["y"]) == (800, 400)


def test_right_click_ladder_uses_ax_show_menu():
    op = {"op": "act", "do": "right_click",
          "target": {"ax": {"title": "New Note"}}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "ax_element"
    args = [a for n, a in d.calls if n == "right_click"][-1]
    assert args.items() >= {
        "pid": 42, "window_id": 7, "element_index": 12}.items()


def test_right_click_pixel_fallback_is_window_local():
    op = {"op": "act", "do": "right_click", "target": {"x": 500, "y": 250}}
    d = FakeDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    args = [a for n, a in d.calls if n == "right_click"][-1]
    assert args.items() >= {"pid": 42, "window_id": 7, "x": 800, "y": 400}.items()


class NoPressDriver(FakeDriver):
    """Element clicks fail hard (Notes list cells: kAXErrorActionUnsupported)."""

    async def tool(self, name, /, **args):
        if name in ("click", "double_click") and "element_index" in args:
            self.calls.append((name, args))
            from axstream.driver import DriverError
            raise DriverError("click: AX action failed: "
                              "AXUIElementPerformAction(AXPress) returned -25206")
        return await super().tool(name, **args)


def test_ax_action_error_falls_through_to_pixel():
    op = {"op": "act", "do": "click",
          "target": {"x": 500, "y": 250, "ax": {"title": "New Note"}}}
    d = NoPressDriver()
    code, events = run([op], d)
    assert code == 0
    assert events[0]["via"] == "window_pixel"
    assert "-25206" in events[0]["note"]
    args = [a for n, a in d.calls if n == "click"][-1]
    assert (args["x"], args["y"]) == (800, 400) and "element_index" not in args


def test_ax_action_error_without_coords_fails():
    op = {"op": "act", "do": "click", "target": {"ax": {"title": "New Note"}}}
    code, events = run([op], NoPressDriver())
    assert code == 1
    assert "AX element click failed" in events[-1]["reason"]


def test_label_only_miss_fails_with_reason():
    op = {"op": "act", "do": "click", "target": {"ax": {"title": "Ghost"}}}
    code, events = run([op], FakeDriver())
    assert code == 1
    final = events[-1]
    assert set(final) == {"failed_at", "op", "reason", "completed"}
    assert "label not found in AX tree" in final["reason"]


def test_no_window_fails_with_reason():
    op = {"op": "act", "do": "click",
          "target": {"x": 1, "y": 2, "ax": {"title": "New Note"}}}
    code, events = run([op], FakeDriver(windows=[]))
    assert code == 1
    assert "no window" in events[-1]["reason"]


def test_window_snapshot_reuses_cached_dims_on_capture_hiccup():
    # window screenshots fail transiently (observed live right after a click);
    # while the bounds are unchanged the previous size still describes the
    # same pixel space, so window_snapshot serves it from cache
    class Flaky(FakeDriver):
        def __init__(self):
            super().__init__()
            self.shots = 0

        async def tool(self, name, /, **args):
            if name == "get_window_state" and "screenshot_out_file" in args:
                self.shots += 1
                if self.shots > 1:
                    self.calls.append((name, args))
                    return {"elements": self.elements}  # capture failed: no dims
            return await super().tool(name, **args)

    d = Flaky()
    s1 = asyncio.run(d.window_snapshot(with_screenshot=True))
    s2 = asyncio.run(d.window_snapshot(with_screenshot=True))
    assert s1.screenshot_size == (1600, 1200)
    assert s2.screenshot_size == (1600, 1200)


def test_window_pixels_from_screen_conversion():
    bounds = {"x": 100, "y": 50, "width": 800, "height": 600}
    assert window_pixels_from_screen(500, 250, bounds, (1600, 1200)) == (800, 400)
    # 1x (non-Retina, no downscale): pixels == points
    assert window_pixels_from_screen(500, 250, bounds, (800, 600)) == (400, 200)


def test_done_stops_replay():
    actions = [
        {"op": "act", "do": "wait", "ms": 1},
        {"op": "done", "status": "success"},
        {"op": "act", "do": "type", "text": "never typed"},
    ]
    computer = MockComputer(latency=0)
    code, events = run(actions, computer)
    assert code == 0
    assert events[-1]["completed"] == 2
    assert all(name != "type_text" for _, name, _ in computer.log)
