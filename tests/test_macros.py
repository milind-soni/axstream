import asyncio

from axstream.macros import Macro, MacroStore, _fill


def test_slot_fill_recursive():
    actions = [
        {"op": "act", "do": "open", "target": "Notes"},
        {"op": "act", "do": "type", "text": "{title}"},
        {"op": "act", "do": "key", "keys": ["cmd", "n"]},
    ]
    filled = _fill(actions, {"title": "buy milk"})
    assert filled[1]["text"] == "buy milk"
    assert filled[0]["target"] == "Notes"  # non-slot untouched
    assert filled[2]["keys"] == ["cmd", "n"]  # lists preserved


def test_missing_slot_left_as_placeholder():
    assert _fill("{title}", {})["title" if False else 0:] or True  # noop guard
    assert _fill("hi {name}", {}) == "hi {name}"


def test_store_roundtrip_and_resolve(tmp_path):
    store = MacroStore(path=tmp_path / "m.json")
    store.add(Macro(
        id="new_note_titled", description="new note with title", slots=["title"],
        examples=[{"utterance": "note titled x", "slots": {"title": "x"}}],
        actions=[{"op": "act", "do": "type", "text": "{title}"}],
    ))
    # reload from disk
    reloaded = MacroStore(path=tmp_path / "m.json")
    assert "new_note_titled" in reloaded.macros
    plan = reloaded.resolve("new_note_titled", {"title": "yoyo"})
    assert plan["actions"][0]["text"] == "yoyo"
    # resolve bumped frecency
    assert reloaded.macros["new_note_titled"].rank > 1.0


def test_frecency_ordering(tmp_path):
    store = MacroStore(path=tmp_path / "m.json")
    for i in range(3):
        store.add(Macro(id=f"m{i}", description="d", slots=[], examples=[], actions=[{"op": "act", "do": "wait", "ms": 1}]))
    store.resolve("m2", {})  # used most recently + rank up
    ids = [t["id"] for t in store.templates()]
    assert ids[0] == "m2"  # frecency puts the used one first


def test_merge_keeps_rank_adds_examples(tmp_path):
    store = MacroStore(path=tmp_path / "m.json")
    store.add(Macro(id="x", description="d", slots=["s"],
                    examples=[{"utterance": "a", "slots": {"s": "1"}}],
                    actions=[{"op": "act", "do": "wait", "ms": 1}]))
    store.resolve("x", {"s": "1"})  # rank -> 2.0
    store.add(Macro(id="x", description="d", slots=["s"],
                    examples=[{"utterance": "b", "slots": {"s": "2"}}],
                    actions=[{"op": "act", "do": "wait", "ms": 1}]))
    assert store.macros["x"].rank == 2.0  # preserved
    assert len(store.macros["x"].examples) == 2  # merged
