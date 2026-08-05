import json
import plistlib
from datetime import datetime, timezone
from pathlib import Path

from axstream import gate, launcher
from axstream.launcher import ReplayResult, Workflow
from axstream.macrofile import dumps, parse


def workflow(name, state="verified"):
    return Workflow(
        name=name,
        description=f"Run {name}",
        when_to_use="",
        slots={"topic": {"description": "Research topic", "example": "WebGPU"}},
        verification=state,
        actions=4,
        path=f"/tmp/{name}.axstream",
        apps=("Safari", "Notes"),
    )


def test_load_workflows_exposes_slots_apps_and_live_trust(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    macro_dir = tmp_path / ".axstream" / "macros"
    macro_dir.mkdir(parents=True)
    mf = parse("\n".join([
        json.dumps({
            "name": "brief",
            "description": "Make a brief",
            "slots": {"topic": {"description": "Subject", "example": "CRDT"}},
        }),
        json.dumps({"op": "act", "do": "open", "target": "Safari"}),
        json.dumps({"op": "act", "do": "open", "target": "Notes"}),
        json.dumps({"op": "assert", "target": {"text": "{topic}"}}),
    ]))
    mf.extra["verified"] = {
        "at": "2026-08-03T00:00:00+00:00",
        "hash": gate.actions_hash(mf),
        "blind_steps": 0,
    }
    (macro_dir / "brief.axstream").write_text(dumps(mf))

    workflows, broken = launcher.load_workflows()

    assert not broken
    assert len(workflows) == 1
    item = workflows[0]
    assert item.verification == "verified"
    assert item.apps == ("Safari", "Notes")
    assert item.slots["topic"]["description"] == "Subject"


def test_rank_workflows_personalizes_inside_the_verified_trust_band():
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    history = [
        {"at": "2026-08-02T00:00:00+00:00", "macro": "recent", "status": "success"},
        {"at": "2026-08-01T00:00:00+00:00", "macro": "draft", "status": "success"},
    ]
    ranked = launcher.rank_workflows(
        [workflow("older"), workflow("recent"), workflow("draft", "unverified")],
        history, now)
    assert [item.name for item in ranked] == ["recent", "older", "draft"]


def test_history_never_persists_slot_values(tmp_path):
    path = tmp_path / "runs.jsonl"
    result = ReplayResult("travel-pack", True, 421, 0, completed=4, total=4)
    launcher.append_history(result, ["city", "traveler_name"], path)

    raw = path.read_text()
    row = json.loads(raw)
    assert row["slots"] == ["city", "traveler_name"]
    assert "Kyoto" not in raw
    assert "Milind" not in raw


def test_load_history_skips_corrupt_rows(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text('{"macro":"ok","status":"success"}\nnot-json\n{}\n')
    assert launcher.load_history(path) == [{"macro": "ok", "status": "success"}]


def test_replay_workflow_streams_progress_and_records_result(tmp_path, monkeypatch):
    lines = iter([
        json.dumps({"i": 0, "ok": True}) + "\n",
        json.dumps({"i": 1, "ok": True}) + "\n",
        json.dumps({"ok": True, "completed": 2, "total": 2}) + "\n",
    ])

    class FakeProcess:
        stdout = lines

        def wait(self):
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    seen = []
    result = launcher.replay_workflow(
        workflow("brief"), {"topic": "private value"}, seen.append)

    assert result.ok
    assert result.completed == 2 and result.total == 2
    assert len(seen) == 3


def test_record_outcome_is_shared_by_replay_surfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "RUN_HISTORY", tmp_path / "unused-default.jsonl")
    written = []
    monkeypatch.setattr(launcher, "append_history",
                        lambda result, slots, path=launcher.RUN_HISTORY:
                        written.append((result, list(slots))))
    result = launcher.record_outcome(
        "brief", 0, 1300, ["topic"], {"ok": True, "completed": 7, "total": 7})
    assert result.ok and result.duration_ms == 1300
    assert written[0][1] == ["topic"]


def test_build_replay_command_uses_file_path_and_json_slots():
    command = launcher.build_replay_command(workflow("brief"), {"topic": "Kyoto cafés"})
    assert command[1:4] == ["-m", "axstream", "replay"]
    assert command[4] == "/tmp/brief.axstream"
    assert json.loads(command[-1]) == {"topic": "Kyoto cafés"}


