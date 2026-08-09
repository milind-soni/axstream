"""MCP tool handlers — regression coverage for dispatch-level crashes that
unit tests of the CLI never see (the `act` tool once referenced a macro
variable that only exists in the replay handler)."""

import json

from axstream import mcp
import axstream.driver as driver_mod
import axstream.replay as replay_mod


class FakeComputer:
    async def connect(self):
        pass

    async def close(self):
        pass

    async def fast_cursor(self):
        pass


def _patch_executor(monkeypatch, code=0):
    monkeypatch.setattr(driver_mod, "DriverComputer", FakeComputer)

    async def fake_run_actions(actions, computer, emit, **kw):
        for i, op in enumerate(actions):
            emit({"i": i, "op": op, "ok": True})
        emit({"ok": True, "completed": len(actions), "total": len(actions)})
        return code

    monkeypatch.setattr(replay_mod, "run_actions", fake_run_actions)


def test_act_tool_executes_ops(monkeypatch):
    _patch_executor(monkeypatch)
    res = mcp._tool_act({"ops": [{"op": "act", "do": "wait", "ms": 1}]})
    assert not res.get("isError")
    lines = [json.loads(l)
             for l in res["content"][0]["text"].strip().splitlines()]
    assert lines[-1]["ok"] is True


def test_act_tool_rejects_invalid_ops():
    res = mcp._tool_act({"ops": [{"op": "act", "do": "nuke"}]})
    assert res.get("isError")
    assert "ops[0] invalid" in res["content"][0]["text"]


def test_act_tool_requires_ops():
    res = mcp._tool_act({"ops": []})
    assert res.get("isError")


def test_act_tool_surfaces_run_failure(monkeypatch):
    _patch_executor(monkeypatch, code=1)
    res = mcp._tool_act({"ops": [{"op": "act", "do": "wait", "ms": 1}]})
    assert res.get("isError")
