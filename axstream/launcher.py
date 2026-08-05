"""Shared model and replay service for Axstream's human-facing launcher.

The menu-bar UI is deliberately thin.  This module owns workflow discovery,
trust state, privacy-preserving run history, frecency ranking, and the replay
subprocess so the behavior is testable without starting AppKit.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .gate import verification
from .macrofile import MacroFileError, discover

RUN_HISTORY = Path("~/.axstream/runs.jsonl")
MAX_HISTORY_LINES = 500


@dataclass(frozen=True)
class Workflow:
    """The small, stable view of a macro needed by launchers."""

    name: str
    description: str
    when_to_use: str
    slots: dict[str, dict]
    verification: str
    actions: int
    path: str
    apps: tuple[str, ...] = ()
    examples: tuple[dict, ...] = ()  # optional matcher few-shots from the file

    @property
    def required_slots(self) -> tuple[str, ...]:
        return tuple(sorted(self.slots))


@dataclass(frozen=True)
class BrokenWorkflow:
    name: str
    path: str
    error: str


@dataclass
class ReplayResult:
    macro: str
    ok: bool
    duration_ms: int
    returncode: int
    completed: int = 0
    total: int = 0
    unverified_steps: list[int] = field(default_factory=list)
    failed_at: Optional[int] = None
    reason: str = ""
    events: list[dict] = field(default_factory=list)


def _opened_apps(actions: Iterable[dict]) -> tuple[str, ...]:
    apps: list[str] = []
    for op in actions:
        if op.get("op") != "act" or op.get("do") != "open":
            continue
        target = op.get("target")
        if isinstance(target, str) and target not in apps:
            apps.append(target)
    return tuple(apps)


def load_workflows() -> tuple[list[Workflow], list[BrokenWorkflow]]:
    """Discover file macros, preserving project-before-user precedence.

    A project macro with the same header name as a user macro is the one the
    replay resolver would choose, so the launcher deduplicates the same way.
    """
    workflows: list[Workflow] = []
    broken: list[BrokenWorkflow] = []
    seen: set[str] = set()
    for path, mf in discover():
        if isinstance(mf, MacroFileError):
            broken.append(BrokenWorkflow(path.stem, str(path), str(mf)))
            continue
        if mf.name in seen:
            continue
        seen.add(mf.name)
        slot_specs = {
            str(name): (dict(spec) if isinstance(spec, dict) else {})
            for name, spec in mf.slots.items()
        }
        workflows.append(Workflow(
            name=mf.name,
            description=mf.description,
            when_to_use=mf.when_to_use,
            slots=slot_specs,
            verification=verification(mf)["state"],
            actions=len(mf.actions),
            path=str(path),
            apps=_opened_apps(mf.actions),
            examples=tuple(mf.examples),
        ))
    return workflows, broken


def load_history(path: Path = RUN_HISTORY, limit: int = MAX_HISTORY_LINES) -> list[dict]:
    path = path.expanduser()
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text().splitlines()[-limit:]
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("macro"), str):
            rows.append(row)
    return rows


def append_history(result: ReplayResult, slot_names: Iterable[str],
                   path: Path = RUN_HISTORY) -> None:
    """Persist an outcome without persisting parameter values or UI text."""
    path = path.expanduser()
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "macro": result.macro,
        "status": "success" if result.ok else "failed",
        "duration_ms": result.duration_ms,
        "completed": result.completed,
        "total": result.total,
        "slots": sorted(set(slot_names)),
    }
    if result.failed_at is not None:
        row["failed_at"] = result.failed_at
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        lines = path.read_text().splitlines()
        if len(lines) > MAX_HISTORY_LINES:
            path.write_text("\n".join(lines[-MAX_HISTORY_LINES:]) + "\n")
    except OSError:
        # Replay success must never be turned into failure by optional history.
        return


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def workflow_score(name: str, history: Iterable[dict],
                   now: Optional[datetime] = None) -> float:
    """A transparent frecency score: successful recent runs dominate.

    Seven-day half-life keeps yesterday's workflow near the top without
    making something used months ago impossible to displace.
    """
    now = now or datetime.now(timezone.utc)
    score = 0.0
    for row in history:
        if row.get("macro") != name:
            continue
        at = _parse_time(row.get("at"))
        if at is None:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - at).total_seconds() / 86400)
        weight = 1.0 if row.get("status") == "success" else 0.15
        score += weight * math.pow(0.5, age_days / 7.0)
    return score


def rank_workflows(workflows: Iterable[Workflow], history: Iterable[dict],
                   now: Optional[datetime] = None) -> list[Workflow]:
    history = list(history)
    # Trust is the hard partition: repetition must never quietly promote a
    # draft above a verified workflow. Frecency personalizes within each band.
    trust = {"verified": 0, "unverified": 1, "stale": 1}
    return sorted(
        workflows,
        key=lambda workflow: (
            trust.get(workflow.verification, 3),
            -workflow_score(workflow.name, history, now),
            workflow.name.casefold(),
        ),
    )


def build_replay_command(workflow: Workflow, slots: dict[str, str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "axstream",
        "replay",
        workflow.path,
        "--slots",
        json.dumps(slots, ensure_ascii=False),
    ]


def replay_workflow(workflow: Workflow, slots: dict[str, str],
                    on_event: Optional[Callable[[dict], None]] = None) -> ReplayResult:
    """Replay in a child process and stream structured progress to the UI."""
    started = time.perf_counter()
    events: list[dict] = []
    fallback_output: list[str] = []
    process = subprocess.Popen(
        build_replay_command(workflow, slots),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            fallback_output.append(line)
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if on_event is not None:
            on_event(event)
    returncode = process.wait()
    duration_ms = round((time.perf_counter() - started) * 1000)
    terminal = events[-1] if events else {}
    ok = returncode == 0 and bool(terminal.get("ok"))
    reason = str(terminal.get("reason") or "")
    if not reason and not ok and fallback_output:
        reason = fallback_output[-1]
    result = ReplayResult(
        macro=workflow.name,
        ok=ok,
        duration_ms=duration_ms,
        returncode=returncode,
        completed=int(terminal.get("completed") or 0),
        total=int(terminal.get("total") or workflow.actions),
        unverified_steps=list(terminal.get("unverified_steps") or []),
        failed_at=terminal.get("failed_at"),
        reason=reason,
        events=events,
    )
    return result


def record_outcome(macro: str, returncode: int, duration_ms: int,
                   slot_names: Iterable[str], terminal: dict) -> ReplayResult:
    """Record an outcome from any replay surface (CLI, MCP, or future API)."""
    ok = returncode == 0 and bool(terminal.get("ok"))
    result = ReplayResult(
        macro=macro,
        ok=ok,
        duration_ms=duration_ms,
        returncode=returncode,
        completed=int(terminal.get("completed") or 0),
        total=int(terminal.get("total") or 0),
        unverified_steps=list(terminal.get("unverified_steps") or []),
        failed_at=terminal.get("failed_at"),
        reason=str(terminal.get("reason") or ""),
    )
    append_history(result, slot_names)
    return result


def result_dict(result: ReplayResult) -> dict:
    """JSON-friendly result for future launchers and tests."""
    return asdict(result)
