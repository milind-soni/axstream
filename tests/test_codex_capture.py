"""Codex native computer-use capture bridge and deterministic compiler."""

import json

import pytest

from axstream import codex_capture
from axstream.codex_capture import (
    CaptureCompileError,
    compile_capture,
    compile_records,
    element_from_state,
)
from axstream.mcp import TOOLS, _tool_begin_capture


TEXTEDIT_STATE = """Window: "Open", App: TextEdit.
0 standard window Open, ID: open-panel, Secondary Actions: Raise
    38 collection Description: icon view, ID: IconView
        52 button New Document, ID: NewDocumentButton
        54 button Cancel, ID: CancelButton
56 menu bar
"""


def test_element_from_native_state_becomes_durable_ax_target():
    assert element_from_state(TEXTEDIT_STATE, 52) == {
        "role": "AXButton", "title": "New Document"}


def test_compile_native_trace_parameterizes_successful_actions():
    records = [
        {"kind": "state", "tool": "get_app_state", "app": "TextEdit",
         "text": TEXTEDIT_STATE},
        {"kind": "action", "tool": "click", "app": "TextEdit",
         "args": {"app": "TextEdit", "element_index": 52},
         "before_state": TEXTEDIT_STATE},
        {"kind": "action", "tool": "press_key", "app": "TextEdit",
         "args": {"app": "TextEdit", "key": "super+n"}},
        {"kind": "action", "tool": "type_text", "app": "TextEdit",
         "args": {"app": "TextEdit", "text": "capture me"}},
    ]
    mf = compile_records(
        {"capture_id": "c-1", "name": "native-note",
         "description": "make a native TextEdit note"},
        records,
        slots={"body": {"value": "capture me", "description": "note body"}},
        terminal_assert={"text": "capture me"},
    )
    assert mf.provenance["source"] == "codex-computer-use-trace"
    assert mf.provenance["captured_slot_hashes"]["body"]
    assert "capture me" not in mf.provenance["captured_slot_hashes"]["body"]
    assert mf.slots["body"]["example"] == "capture me"
    assert mf.actions == [
        {"op": "act", "do": "open", "target": "TextEdit"},
        {"op": "act", "do": "click",
         "target": {"ax": {"role": "AXButton", "title": "New Document"}}},
        {"op": "act", "do": "key", "keys": ["cmd", "n"]},
        {"op": "act", "do": "type", "text": "{body}"},
        {"op": "assert", "target": {"text": "{body}"}},
        {"op": "done", "status": "success"},
    ]


def test_compile_set_value_uses_portable_click_select_type_sequence():
    record = {
        "kind": "action", "tool": "set_value", "app": "Safari",
        "args": {"app": "Safari", "element_index": 8, "value": "webgpu"},
        "element": {"role": "text field", "title": "Address and Search"},
    }
    mf = compile_records({"name": "search"}, [record])
    assert mf.actions[1:4] == [
        {"op": "act", "do": "click",
         "target": {"ax": {"role": "AXTextField",
                            "title": "Address and Search"}}},
        {"op": "act", "do": "key", "keys": ["cmd", "a"]},
        {"op": "act", "do": "type", "text": "webgpu"},
    ]


def test_compile_preserves_right_click_and_show_menu():
    target = {"role": "row", "title": "file.txt"}
    records = [
        {"kind": "action", "tool": "click", "app": "Finder",
         "args": {"app": "Finder", "element_index": 2,
                  "mouse_button": "right"},
         "element": target},
        {"kind": "action", "tool": "perform_secondary_action",
         "app": "Finder",
         "args": {"app": "Finder", "element_index": 2,
                  "action": "Show Menu"},
         "element": target},
    ]
    mf = compile_records({"name": "menus"}, records)
    assert mf.actions[1:3] == [
        {"op": "act", "do": "right_click",
         "target": {"ax": {"role": "AXRow", "title": "file.txt"}}},
        {"op": "act", "do": "right_click",
         "target": {"ax": {"role": "AXRow", "title": "file.txt"}}},
    ]


@pytest.mark.parametrize("args, message", [
    ({"mouse_button": "middle"}, "middle-click"),
    ({"mouse_button": "right", "click_count": 2}, "right-button double-click"),
    ({"mouse_button": "left", "click_count": 3}, "click_count=3"),
])
def test_compile_refuses_unfaithful_click_variants(args, message):
    record = {"kind": "action", "tool": "click", "app": "Finder",
              "args": {"app": "Finder", "x": 1, "y": 2, **args}}
    with pytest.raises(CaptureCompileError, match=message):
        compile_records({"name": "bad-click"}, [record])


def test_compile_refuses_unknown_secondary_action():
    record = {
        "kind": "action", "tool": "perform_secondary_action", "app": "Finder",
        "args": {"app": "Finder", "element_index": 2, "action": "Increment"},
        "element": {"role": "row", "title": "file.txt"},
    }
    with pytest.raises(CaptureCompileError, match="Increment"):
        compile_records({"name": "bad-secondary"}, [record])


def test_compile_refuses_lossy_element_index_without_state():
    record = {"kind": "action", "tool": "click",
              "args": {"app": "Notes", "element_index": 44}}
    with pytest.raises(CaptureCompileError, match="get_app_state"):
        compile_records({"name": "bad"}, [record])


def test_compile_coordinate_click_uses_source_screenshot_fraction():
    record = {
        "kind": "action", "tool": "click", "app": "TextEdit",
        "args": {"app": "TextEdit", "x": 293, "y": 244},
        "before_screenshot_size": {"width": 586, "height": 488},
    }
    mf = compile_records({"name": "coordinate-click"}, [record])
    assert mf.actions[1] == {
        "op": "act", "do": "click",
        "target": {"win": {"fx": 0.5, "fy": 0.5,
                           "w": 586, "h": 488}},
    }


def test_compile_coordinate_click_refuses_missing_source_dimensions():
    record = {"kind": "action", "tool": "click", "app": "TextEdit",
              "args": {"app": "TextEdit", "x": 293, "y": 244}}
    with pytest.raises(CaptureCompileError, match="screenshot dimensions"):
        compile_records({"name": "bad-coordinate"}, [record])


def test_compile_drag_is_resize_and_retina_aware():
    record = {
        "kind": "action", "tool": "drag", "app": "TextEdit",
        "args": {"app": "TextEdit", "from_x": 58.6, "from_y": 48.8,
                 "to_x": 527.4, "to_y": 439.2},
        "before_screenshot_size": {"width": 586, "height": 488},
    }
    mf = compile_records({"name": "drag"}, [record])
    assert mf.actions[1] == {
        "op": "act", "do": "drag",
        "from": {"win": {"fx": pytest.approx(0.1),
                          "fy": pytest.approx(0.1), "w": 586, "h": 488}},
        "to": {"win": {"fx": pytest.approx(0.9),
                        "fy": pytest.approx(0.9), "w": 586, "h": 488}},
    }


def test_compile_refuses_unsupported_native_action():
    record = {"kind": "action", "tool": "select_text",
              "args": {"app": "TextEdit", "element_index": 2,
                       "text": "hello"},
              "element": {"role": "text entry area"}}
    with pytest.raises(CaptureCompileError, match="unsupported native tool"):
        compile_records({"name": "bad"}, [record])


def test_failed_native_actions_are_not_compiled():
    records = [
        {"kind": "failed_action", "tool": "click",
         "args": {"app": "Notes", "x": 1, "y": 2}},
        {"kind": "action", "tool": "type_text",
         "args": {"app": "Notes", "text": "worked"}},
    ]
    mf = compile_records({"name": "only-success"}, records)
    assert [op.get("do") for op in mf.actions if op.get("op") == "act"] == [
        "open", "type"]


def test_begin_and_compile_capture_file(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_capture, "CAPTURE_DIR", tmp_path)
    started = codex_capture.begin_capture("Captured Task", "a description")
    path = tmp_path / f"{started['capture_id']}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps({
            "kind": "action", "tool": "type_text",
            "args": {"app": "TextEdit", "text": "hello"},
        }) + "\n")
    mf = compile_capture(started["capture_id"],
                         terminal_assert={"text": "hello"})
    assert mf.name == "captured-task"
    assert mf.description == "a description"
    assert mf.actions[-2] == {"op": "assert", "target": {"text": "hello"}}


def test_mcp_exposes_capture_bridge_and_returns_node_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_capture, "CAPTURE_DIR", tmp_path)
    names = {tool["name"] for tool in TOOLS}
    assert {"begin_capture", "compile_capture"} <= names
    result = _tool_begin_capture({"name": "native test"})
    assert not result.get("isError")
    payload = json.loads(result["content"][0]["text"])
    assert payload["capture_id"].startswith("native-test-")
    assert "wrapSkyForAxstream" in payload["node_setup"]
    assert "unwrapSkyForAxstream" in payload["node_teardown"]
    assert payload["trace_path"].startswith(str(tmp_path))


def test_compile_seeds_a_spoken_example_for_the_voice_matcher():
    # The fine-tuned matcher never selects example-less templates, so every
    # compiled capture must arrive voice-matchable: one seeded example whose
    # slot values appear verbatim in the utterance, lowercase.
    records = [
        {"kind": "action", "tool": "type_text", "app": "TextEdit",
         "args": {"app": "TextEdit", "text": "capture me"}},
    ]
    mf = compile_records(
        {"capture_id": "c-2", "name": "native-note",
         "description": "Make a native TextEdit note.",
         "when_to_use": "User wants a quick TextEdit note."},
        records,
        slots={"body": {"value": "capture me", "description": "note body"}},
    )
    assert len(mf.examples) == 1
    example = mf.examples[0]
    utterance = example["utterance"]
    assert utterance == utterance.lower()
    assert example["slots"] == {"body": "capture me"}
    for value in example["slots"].values():
        assert value.lower() in utterance
    assert "user wants a quick textedit note" in utterance
