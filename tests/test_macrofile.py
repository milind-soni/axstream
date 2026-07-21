"""Macro files: parse/serialize round-trip, slot fill, discovery."""

import json

import pytest

from axstream.macrofile import (
    MacroFile,
    MacroFileError,
    dumps,
    from_macro,
    load,
    parse,
    resolve_name,
    save,
    to_macro,
)
from axstream.macros import Macro

HEADER = {
    "name": "new_note_titled",
    "description": "open a new note and type a title",
    "when_to_use": "user wants a fresh note with given text",
    "slots": {"title": {"description": "the note title", "example": "standup"}},
    "provenance": {"source": "hand-written", "created": "2026-07-22"},
}
ACTIONS = [
    {"op": "act", "do": "open", "target": "Notes"},
    {"op": "act", "do": "key", "keys": ["cmd", "n"]},
    {"op": "act", "do": "wait", "ms": 400},
    {"op": "act", "do": "type", "text": "{title}"},
]


def make_text(header=HEADER, actions=ACTIONS):
    lines = ([json.dumps(header)] if header else []) + [json.dumps(a) for a in actions]
    return "\n".join(lines) + "\n"


def test_round_trip(tmp_path):
    mf = parse(make_text())
    p = save(mf, tmp_path / "new_note_titled.axstream")
    again = load(p)
    assert again.name == "new_note_titled"
    assert again.description == HEADER["description"]
    assert again.when_to_use == HEADER["when_to_use"]
    assert again.slots == HEADER["slots"]
    assert again.provenance == HEADER["provenance"]
    assert again.actions == ACTIONS
    # serialization is stable
    assert dumps(again) == dumps(mf)


def test_raw_draft_without_header(tmp_path):
    p = tmp_path / "draft.axstream"
    p.write_text(make_text(header=None))
    mf = load(p)
    assert mf.name == "draft"  # falls back to the file stem
    assert mf.actions == ACTIONS
    assert mf.slots == {}


def test_provenance_only_header():
    """The SupaMaus export case: a small provenance header, nothing else."""
    header = {"provenance": {"source": "supamaus-recording", "capture_id": "c_42",
                             "created": "2026-07-22T10:00:00Z"}}
    mf = parse(make_text(header=header), name_hint="recorded")
    assert mf.name == "recorded"
    assert mf.provenance["source"] == "supamaus-recording"
    assert mf.provenance["capture_id"] == "c_42"


def test_combined_ax_and_coordinate_click_target():
    """Draft clicks carry BOTH coordinates and the AX label — must validate."""
    op = {"op": "act", "do": "click",
          "target": {"x": 420, "y": 312, "ax": {"title": "Save"}}}
    mf = parse(make_text(header=None, actions=[op]))
    assert mf.actions[0]["target"]["ax"]["title"] == "Save"


def test_slot_fill():
    mf = parse(make_text())
    filled = mf.fill({"title": "buy milk"})
    assert filled[3]["text"] == "buy milk"
    assert filled[0]["target"] == "Notes"
    assert mf.actions[3]["text"] == "{title}"  # original untouched


def test_missing_declared_slot_errors():
    mf = parse(make_text())
    with pytest.raises(MacroFileError, match="title"):
        mf.fill({})


def test_undeclared_braces_are_literal():
    """No header slots -> {word} is literal text, not a slot; never errors."""
    actions = [{"op": "act", "do": "type", "text": "template {x} stays"}]
    mf = parse(make_text(header=None, actions=actions))
    assert mf.fill({}) == actions


def test_invalid_json_line_reports_line_number():
    with pytest.raises(MacroFileError, match="line 2"):
        parse(json.dumps(HEADER) + "\n{not json\n")


def test_invalid_op_reports_line_number():
    bad = json.dumps(HEADER) + "\n" + json.dumps({"op": "act", "do": "teleport"}) + "\n"
    with pytest.raises(MacroFileError, match="line 2.*teleport"):
        parse(bad)


def test_comments_and_blank_lines_skipped():
    text = "# a comment\n\n" + make_text()
    mf = parse(text)
    assert len(mf.actions) == len(ACTIONS)


def test_shorthand_ops_normalized():
    mf = parse(make_text(header=None, actions=[{"op": "type", "text": "hi"}]))
    assert mf.actions[0] == {"op": "act", "do": "type", "text": "hi"}


def test_empty_file_errors():
    with pytest.raises(MacroFileError, match="no action lines"):
        parse("# nothing here\n")


def test_sidecar_header(tmp_path):
    p = tmp_path / "sidecar.axstream"
    p.write_text(make_text(header=None))
    (tmp_path / "sidecar.json").write_text(json.dumps(HEADER))
    mf = load(p)
    assert mf.name == "new_note_titled"
    assert mf.slots == HEADER["slots"]


def test_resolve_name_by_stem_and_path(tmp_path):
    d = tmp_path / "macros"
    d.mkdir()
    p = save(parse(make_text()), d / "new_note_titled.axstream")
    assert resolve_name("new_note_titled", dirs=[d]) == p
    assert resolve_name(str(p), dirs=[]) == p
    assert resolve_name("nope", dirs=[d]) is None


def test_resolve_name_by_header_name(tmp_path):
    d = tmp_path / "macros"
    d.mkdir()
    p = save(parse(make_text()), d / "different_filename.axstream")
    assert resolve_name("new_note_titled", dirs=[d]) == p


def test_store_bridge_round_trip():
    mf = parse(make_text())
    m = to_macro(mf)
    assert isinstance(m, Macro)
    assert m.id == "new_note_titled"
    assert m.slots == ["title"]
    assert m.actions == ACTIONS
    back = from_macro(m)
    assert back.name == m.id
    assert back.actions == ACTIONS
    assert set(back.slots) == {"title"}
