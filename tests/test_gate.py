"""The verify-before-store gate + task-family dedup (gate.py)."""

import json

from axstream import gate
from axstream.macrofile import parse

BASE = [
    {"op": "act", "do": "open", "target": "Safari"},
    {"op": "act", "do": "key", "keys": ["cmd", "t"]},
    {"op": "act", "do": "type", "text": "{query}"},
    {"op": "act", "do": "key", "keys": ["enter"]},
    {"op": "assert", "target": {"text": "{query}"}},
    {"op": "done", "status": "success"},
]


def mf_of(actions, name="m", header=None):
    lines = [json.dumps({"name": name, **(header or {})})]
    lines += [json.dumps(op) for op in actions]
    return parse("\n".join(lines))


# -- signature --------------------------------------------------------------

def test_signature_ignores_waits_and_names():
    a = mf_of(BASE)
    b_actions = [dict(op) for op in BASE]
    b_actions.insert(2, {"op": "act", "do": "wait", "ms": 900})
    b = mf_of(b_actions, name="other-name")
    assert gate.signature(a) == gate.signature(b)


def test_signature_distinguishes_tasks():
    a = mf_of(BASE)
    c_actions = [{"op": "act", "do": "open", "target": "Notes"}] + BASE[1:]
    assert gate.signature(a) != gate.signature(mf_of(c_actions))


# -- gate predicates --------------------------------------------------------

def test_terminal_assert_detection():
    assert gate.terminal_assert(mf_of(BASE))
    no_assert = [op for op in BASE if op["op"] != "assert"]
    assert not gate.terminal_assert(mf_of(no_assert))
    # an assert in the MIDDLE is a guard, not an outcome check
    mid = BASE[:1] + [{"op": "assert", "target": {"text": "x"}}] + BASE[1:4] + BASE[5:]
    assert not gate.terminal_assert(mf_of(mid))


def test_risky_ops_block_verify(tmp_path):
    actions = BASE[:4] + [{"op": "act", "do": "key", "keys": ["cmd", "q"],
                           "risk": "risky"}] + BASE[4:]
    p = tmp_path / "risky.axstream"
    p.write_text("\n".join([json.dumps({"name": "risky"})]
                           + [json.dumps(op) for op in actions]) + "\n")
    result = gate.verify(str(p))
    assert not result["ok"] and "risky" in result["reason"]


def test_verify_requires_terminal_assert(tmp_path):
    actions = [op for op in BASE if op["op"] != "assert"]
    p = tmp_path / "blind.axstream"
    p.write_text("\n".join([json.dumps({"name": "blind"})]
                           + [json.dumps(op) for op in actions]) + "\n")
    result = gate.verify(str(p))
    assert not result["ok"] and "terminal assert" in result["reason"]


# -- the live gate (replay stubbed) ----------------------------------------

def test_verify_stamps_on_success(tmp_path, monkeypatch):
    header = {"slots": {"query": {"example": "weather"}}}
    p = tmp_path / "ok.axstream"
    p.write_text("\n".join([json.dumps({"name": "ok", **header})]
                           + [json.dumps(op) for op in BASE]) + "\n")

    async def fake_replay(actions, emit, delivery=None):
        emit({"ok": True, "completed": len(actions), "total": len(actions),
              "unverified_steps": [1]})
        return 0

    monkeypatch.setattr(gate, "_replay_once", fake_replay)
    result = gate.verify(str(p))
    assert result["ok"] and result["blind_steps"] == 1
    from axstream.macrofile import load

    stamped = load(p)
    assert gate.verification(stamped)["state"] == "verified"
    # editing the actions invalidates the stamp
    stamped.actions[2]["text"] = "changed"
    assert gate.verification(stamped)["state"] == "stale"


def test_verify_fails_on_replay_failure(tmp_path, monkeypatch):
    p = tmp_path / "bad.axstream"
    p.write_text("\n".join([json.dumps({"name": "bad",
                                        "slots": {"query": {"example": "x"}}})]
                           + [json.dumps(op) for op in BASE]) + "\n")

    async def fake_replay(actions, emit, delivery=None):
        emit({"failed_at": 4, "op": actions[4], "reason": "assert failed",
              "completed": 4})
        return 1

    monkeypatch.setattr(gate, "_replay_once", fake_replay)
    result = gate.verify(str(p))
    assert not result["ok"] and result["failed_at"] == 4
    from axstream.macrofile import load

    assert gate.verification(load(p))["state"] == "unverified"


def test_captured_macro_verification_requires_fresh_slot_value(tmp_path, monkeypatch):
    header = {
        "name": "captured",
        "slots": {"query": {"example": "first run"}},
        "provenance": {
            "source": "codex-computer-use-trace",
            "captured_slot_hashes": {
                "query": gate.slot_value_hash("first run")},
        },
    }
    p = tmp_path / "captured.axstream"
    p.write_text("\n".join([json.dumps(header)]
                             + [json.dumps(op) for op in BASE]) + "\n")
    called = {"replay": False}

    async def fake_replay(actions, emit, delivery=None):
        called["replay"] = True
        emit({"ok": True, "completed": len(actions), "total": len(actions)})
        return 0

    monkeypatch.setattr(gate, "_replay_once", fake_replay)
    stale = gate.verify(str(p), {"query": "first run"})
    assert not stale["ok"] and "fresh slot" in stale["reason"]
    assert not called["replay"]

    fresh = gate.verify(str(p), {"query": "second run"})
    assert fresh["ok"] and called["replay"]


# -- upsert-by-signature ----------------------------------------------------

def test_upsert_conflicts_and_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "USER_DIR", tmp_path)
    old = tmp_path / "safari-new-tab.axstream"
    old.write_text("\n".join([json.dumps({"name": "safari-new-tab"})]
                             + [json.dumps(op) for op in BASE]) + "\n")
    new = mf_of(BASE, name="open-safari-new-tab")
    conflicts = gate.upsert_conflicts(new, exclude_stem="open-safari-new-tab")
    assert conflicts == [old]
    dest = gate.archive(old)
    assert not old.exists() and dest.exists()
    assert dest.parent.name == gate.ARCHIVE_DIR_NAME


def test_signature_distinguishes_literal_typed_text():
    # same key/type shape, but typing a literal URL is a DIFFERENT task from
    # typing a slotted query — the upsert must not collapse them
    nav = [dict(op) for op in BASE]
    nav[2] = {"op": "act", "do": "type", "text": "https://example.com/docs"}
    assert gate.signature(mf_of(BASE)) != gate.signature(mf_of(nav))
    # but two instances of the same slotted task still collide (upsert works)
    assert gate.signature(mf_of(BASE)) == gate.signature(mf_of(BASE, name="twin"))
