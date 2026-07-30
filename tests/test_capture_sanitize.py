from axstream.capture import content_derived, sanitize

# The real failure this guards against, from a live SupaMaus recording of
# "create a note, type text, select it, bold it": the recorder sampled the
# note's own text as the clicked element's label — mid-keystroke, so the
# labels are truncated and space-mangled variants of the typed text. Replayed
# with a different slot value they can never match, so every click silently
# fell through to stale coordinates.
RECORDED = [
    {"op": "act", "do": "open", "target": "Notes"},
    {"op": "act", "do": "click", "target": {"ax": {"title": "New Note"}, "x": 563.2, "y": 138.3}},
    {"op": "act", "do": "type", "text": "{note_text}"},
    {"op": "act", "do": "click",
     "target": {"ax": {"title": "Hey there this is a new w"}, "x": 610.1, "y": 207.0}},
    {"op": "act", "do": "click",
     "target": {"ax": {"title": "Hey therethis is a new w"}, "x": 610.1, "y": 207.0}},
    {"op": "act", "do": "key", "keys": ["cmd", "b"]},
]


def test_real_recording_drops_content_labels_keeps_real_button():
    typed = ["Hey there this is a new workflow"]
    actions = [dict(a) for a in RECORDED]
    actions[2] = {"op": "act", "do": "type", "text": typed[0]}
    cleaned, notes = sanitize(actions)

    # the genuine toolbar button keeps its label
    assert cleaned[1]["target"]["ax"]["title"] == "New Note"
    # both content-derived labels are gone, coordinates preserved
    for i in (3, 4):
        assert "ax" not in cleaned[i]["target"], cleaned[i]
        assert cleaned[i]["target"]["x"] == 610.1
    assert len(notes) == 2


def test_short_labels_are_never_content():
    # "Save"/"OK" can coincide with typed text but are real selectors
    assert not content_derived("Save", ["Save the world"])
    assert not content_derived("OK", ["OK then"])


def test_truncated_and_mangled_variants_detected():
    typed = ["reliability test one two three"]
    assert content_derived("reliability test o", typed)      # truncated
    assert content_derived("reliabilitytest one", typed)      # space mangled
    assert not content_derived("Bold Selection", typed)       # unrelated UI label


def test_label_kept_when_no_coords_to_fall_back_on():
    # a doomed lookup still beats having no target at all
    actions = [
        {"op": "act", "do": "type", "text": "some long typed sentence"},
        {"op": "act", "do": "click", "target": {"ax": {"title": "some long typed sentence"}}},
    ]
    cleaned, notes = sanitize(actions)
    assert cleaned[1]["target"]["ax"]["title"] == "some long typed sentence"
    assert notes == []


def test_slot_placeholder_text_marks_labels_as_content():
    # the typed text VARIES per run, so any label sampled from it is content
    actions = [
        {"op": "act", "do": "type", "text": "{note_text}"},
        {"op": "act", "do": "click",
         "target": {"ax": {"title": "{note_text}"}, "x": 1.0, "y": 2.0}},
    ]
    cleaned, _ = sanitize(actions)
    assert "ax" not in cleaned[1]["target"]


def test_sanitize_is_non_mutating():
    original = [dict(a) for a in RECORDED]
    sanitize(RECORDED)
    assert RECORDED == original
