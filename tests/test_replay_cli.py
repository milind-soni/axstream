"""`axstream replay`: --dry, structured progress, and the failure handoff JSON."""

import asyncio
import json

from axstream.computer import MockComputer
from axstream.macrofile import parse
from axstream.replay import cmd_list, cmd_replay, run_actions

FIXTURE = "\n".join([
    json.dumps({"name": "note", "description": "type a note",
                "slots": {"title": {"description": "text to type"}}}),
    json.dumps({"op": "act", "do": "type", "text": "{title}"}),
    json.dumps({"op": "act", "do": "wait", "ms": 1}),
]) + "\n"

# an AX tree with one Save button at (100,200) size 20x10 -> center (110,205)
AX_FIXTURE = {
    "windows": [{"title": "Doc", "children": [{
        "role": "AXButton", "name": "Save", "enabled": True,
        "absolute_position": "100;200", "size": "20;10", "children": [],
    }]}],
    "menubar_items": [], "dock_items": [],
}


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


def test_cli_dry_missing_slot_is_usage_error(tmp_path, capsys):
    p = tmp_path / "note.axstream"
    p.write_text(FIXTURE)
    code = cmd_replay([str(p), "--dry"])
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
    d = tmp_path / ".axstream" / "macros"
    d.mkdir(parents=True)
    (d / "note.axstream").write_text(FIXTURE)
    assert cmd_list(["--json"]) == 0
    row = json.loads(capsys.readouterr().out.strip())
    assert row["name"] == "note"
    assert row["slots"] == ["title"]
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


def test_click_resolves_ax_label_first():
    op = {"op": "act", "do": "click",
          "target": {"x": 5, "y": 5, "ax": {"role": "AXButton", "title": "Save"}}}
    computer = MockComputer(latency=0, ax_fixture=AX_FIXTURE)
    code, events = run([op], computer)
    assert code == 0
    assert events[0]["via"] == "ax"
    assert events[0]["resolved"] == "AXButton 'Save'"
    name, params = computer.log[-1][1], computer.log[-1][2]
    assert name == "left_click" and (params["x"], params["y"]) == (110, 205)


def test_click_falls_back_to_coordinates():
    op = {"op": "act", "do": "click",
          "target": {"x": 5, "y": 7, "ax": {"role": "AXButton", "title": "Save"}}}
    computer = MockComputer(latency=0)  # empty tree: label can't resolve
    code, events = run([op], computer)
    assert code == 0
    assert events[0]["via"] == "coords_fallback"
    name, params = computer.log[-1][1], computer.log[-1][2]
    assert name == "left_click" and (params["x"], params["y"]) == (5, 7)


def test_pure_coordinate_click():
    op = {"op": "act", "do": "click", "target": {"x": 3, "y": 4}}
    code, events = run([op], MockComputer(latency=0))
    assert code == 0
    assert events[0]["via"] == "coords"


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
