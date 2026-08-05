"""Bridge Codex native computer-use traces into replayable Axstream macros.

Codex's ``sky`` computer-use runtime is a separate execution backend from
``cua-driver``.  Enabling cua-driver's trajectory recorder therefore does not
observe native Codex actions.  This module provides the explicit bridge:

* :func:`begin_capture` creates a JSONL trace file.
* ``codex_bridge.mjs`` wraps ``sky`` and appends successful native actions,
  together with the latest accessibility state used to choose them.
* :func:`compile_capture` turns that trace into normal Axstream spec ops.

The bridge deliberately refuses actions it cannot translate faithfully.  A
partial macro that reports success is worse than a capture that asks the agent
to keep using native computer use for the unsupported step.
"""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .macrofile import MacroFile
from .spec import validate_op

CAPTURE_SCHEMA = 1
CAPTURE_DIR = Path("~/.axstream/captures").expanduser()


class CaptureCompileError(ValueError):
    """A native trace cannot be converted without guessing."""


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise CaptureCompileError("capture name must contain a letter or number")
    return slug


def begin_capture(name: str, description: str = "",
                  when_to_use: str = "") -> dict:
    """Create a capture file and return the bridge configuration."""
    stem = _slug(name)
    now = datetime.now(timezone.utc)
    capture_id = f"{stem}-{now:%Y%m%dt%H%M%Sz}-{uuid.uuid4().hex[:8]}"
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = CAPTURE_DIR / f"{capture_id}.jsonl"
    header = {
        "kind": "capture",
        "schema": CAPTURE_SCHEMA,
        "capture_id": capture_id,
        "name": stem,
        "description": description,
        "when_to_use": when_to_use,
        "created": now.isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(header, ensure_ascii=False) + "\n")
    return {
        "capture_id": capture_id,
        "trace_path": str(path),
        "bridge_path": str(Path(__file__).with_name("codex_bridge.mjs")),
    }


def resolve_capture(capture_id: str) -> Path:
    """Resolve only files inside Axstream's capture directory."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", capture_id or ""):
        raise CaptureCompileError("invalid capture id")
    path = CAPTURE_DIR / f"{capture_id}.jsonl"
    if not path.is_file():
        raise CaptureCompileError(f"no capture named {capture_id!r}")
    return path


def read_capture(path: str | Path) -> tuple[dict, list[dict]]:
    """Read a bridge trace, validating its header and record shape."""
    path = Path(path).expanduser()
    try:
        raw_lines = path.read_text().splitlines()
    except OSError as exc:
        raise CaptureCompileError(f"cannot read capture {path}: {exc}") from exc
    rows: list[dict] = []
    for lineno, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CaptureCompileError(
                f"capture line {lineno}: invalid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise CaptureCompileError(f"capture line {lineno}: expected an object")
        rows.append(row)
    if not rows or rows[0].get("kind") != "capture":
        raise CaptureCompileError("capture is missing its header")
    if rows[0].get("schema") != CAPTURE_SCHEMA:
        raise CaptureCompileError(
            f"unsupported capture schema {rows[0].get('schema')!r}")
    return rows[0], rows[1:]


_ROLES = [
    ("search text field", "AXTextField"),
    ("text entry area", "AXTextArea"),
    ("pop up button", "AXPopUpButton"),
    ("menu bar item", "AXMenuBarItem"),
    ("radio button", "AXRadioButton"),
    ("menu button", "AXMenuButton"),
    ("menu item", "AXMenuItem"),
    ("standard window", "AXWindow"),
    ("text field", "AXTextField"),
    ("combo box", "AXComboBox"),
    ("scroll area", "AXScrollArea"),
    ("checkbox", "AXCheckBox"),
    ("check box", "AXCheckBox"),
    ("button", "AXButton"),
    ("row", "AXRow"),
    ("image", "AXImage"),
    ("link", "AXLink"),
    ("text", "AXStaticText"),
]


def _normalise_role(role: str) -> str:
    role = (role or "").strip()
    if role.startswith("AX"):
        return role
    folded = role.casefold()
    for native, ax_role in _ROLES:
        if folded == native:
            return ax_role
    return role


def element_from_state(state_text: str, element_index: int) -> Optional[dict]:
    """Extract a durable role/title selector from a ``sky`` AX text tree."""
    line = None
    pattern = re.compile(rf"^\s*{int(element_index)}\s+(.+)$")
    for candidate in (state_text or "").splitlines():
        match = pattern.match(candidate)
        if match:
            line = match.group(1).strip()
            break
    if line is None:
        return None

    role_name = ax_role = ""
    folded = line.casefold()
    for native, candidate_role in _ROLES:
        if folded.startswith(native):
            role_name, ax_role = native, candidate_role
            break
    if not ax_role:
        return None

    rest = line[len(role_name):].lstrip()
    while rest.startswith("(") and ")" in rest:
        rest = rest[rest.index(")") + 1:].lstrip()

    description = re.search(r"(?:^|,\s*)Description:\s*([^,]+)", rest)
    value = re.search(r"(?:^|,\s*)Value:\s*([^,]+)", rest)
    direct = rest.split(",", 1)[0].strip()
    if direct.startswith(("Description:", "Value:", "Help:", "ID:")):
        direct = ""
    title = (description.group(1).strip() if description else direct)
    if not title and value:
        title = value.group(1).strip()
    if not title:
        return {"role": ax_role}
    return {"role": ax_role, "title": title}


def _target(record: dict, args: dict) -> dict:
    explicit = record.get("element")
    ax: Optional[dict] = None
    if isinstance(explicit, dict):
        role = _normalise_role(str(explicit.get("role") or ""))
        title = str(explicit.get("title") or explicit.get("label") or "").strip()
        ax = {**({"role": role} if role else {}),
              **({"title": title} if title else {})} or None
    elif isinstance(args.get("element_index"), int):
        ax = element_from_state(str(record.get("before_state") or ""),
                                args["element_index"])

    target: dict[str, Any] = {}
    if isinstance(args.get("x"), (int, float)) \
            and isinstance(args.get("y"), (int, float)):
        try:
            target["win"] = _captured_window_point(
                record, args["x"], args["y"])
        except CaptureCompileError:
            # A semantic element selector remains faithful without the
            # coordinate fallback. A coordinate-only click does not.
            if ax is None:
                raise
    if ax:
        target["ax"] = ax
    if not target:
        index = args.get("element_index")
        suffix = (f" element_index={index}" if index is not None else "")
        raise CaptureCompileError(
            "native action has no replayable target" + suffix
            + "; call get_app_state immediately before the action or include "
              "an explicit element {role,title}")
    return target


def _captured_window_point(record: dict, x: Any, y: Any) -> dict:
    """Sky screenshot pixels -> resize-aware Axstream window fraction.

    Native macOS ``sky`` coordinates are measured in its app-window image
    (commonly 1x logical size), while cua-driver's replay image is commonly
    Retina 2x. Persisting the source image dimensions lets replay scale the
    point without ever storing the screenshot itself.
    """
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise CaptureCompileError("coordinate action has non-numeric x/y")
    size = record.get("before_screenshot_size")
    if not isinstance(size, dict):
        raise CaptureCompileError(
            "coordinate action is missing its source screenshot dimensions; "
            "call get_app_state immediately before the action")
    width, height = size.get("width"), size.get("height")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)) \
            or width <= 0 or height <= 0:
        raise CaptureCompileError("coordinate action has invalid screenshot dimensions")
    if x < 0 or y < 0 or x > width or y > height:
        raise CaptureCompileError(
            f"coordinate ({x}, {y}) falls outside captured screenshot "
            f"{width}x{height}")
    return {"fx": x / width, "fy": y / height,
            "w": width, "h": height}


_KEY_NAMES = {
    "super": "cmd", "meta": "cmd", "command": "cmd", "cmd": "cmd",
    "control": "ctrl", "ctrl": "ctrl", "alt": "option", "option": "option",
    "return": "enter", "escape": "esc", "backspace": "delete",
}


def _keys(value: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[+-]", value or "") if p.strip()]
    if not parts:
        raise CaptureCompileError("press_key trace has an empty key")
    return [_KEY_NAMES.get(part.casefold(), part.lower()) for part in parts]


def _replace_strings(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for observed, placeholder in replacements:
            value = value.replace(observed, placeholder)
        return value
    if isinstance(value, list):
        return [_replace_strings(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: _replace_strings(v, replacements) for k, v in value.items()}
    return value


def _slot_specs(slots: Optional[dict]) -> tuple[
        dict, list[tuple[str, str]], dict[str, str]]:
    header: dict[str, dict] = {}
    replacements: list[tuple[str, str]] = []
    observed_hashes: dict[str, str] = {}
    for name, raw in (slots or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
            raise CaptureCompileError(f"invalid slot name {name!r}")
        spec = dict(raw) if isinstance(raw, dict) else {"value": str(raw)}
        observed = spec.pop("value", spec.get("example"))
        if observed in (None, ""):
            raise CaptureCompileError(
                f"slot {name!r} needs a captured value (value or example)")
        observed = str(observed)
        spec.setdefault("example", observed)
        header[str(name)] = spec
        replacements.append((observed, "{" + str(name) + "}"))
        observed_hashes[str(name)] = hashlib.sha256(observed.encode()).hexdigest()[:16]
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
    return header, replacements, observed_hashes


def compile_records(header: dict, records: Iterable[dict], *,
                    name: str = "", description: str = "",
                    when_to_use: str = "", slots: Optional[dict] = None,
                    terminal_assert: Optional[dict] = None) -> MacroFile:
    """Compile successful ``sky`` action records into an Axstream macro."""
    actions: list[dict] = []
    current_app = ""

    def switch(app: str) -> None:
        nonlocal current_app
        if app and app != current_app:
            actions.append({"op": "act", "do": "open", "target": app})
            current_app = app

    for position, record in enumerate(records, start=1):
        if record.get("kind") != "action":
            continue
        tool = str(record.get("tool") or "")
        args = record.get("args") or {}
        if not isinstance(args, dict):
            raise CaptureCompileError(f"record {position}: args must be an object")
        app = str(args.get("app") or record.get("app") or "").strip()
        switch(app)
        try:
            if tool == "click":
                button = str(args.get("mouse_button") or "left").casefold()
                button = {"l": "left", "r": "right", "m": "middle"}.get(
                    button, button)
                count = int(args.get("click_count") or 1)
                if button == "middle":
                    raise CaptureCompileError(
                        "middle-click has no faithful Axstream replay action")
                if button not in {"left", "right"}:
                    raise CaptureCompileError(
                        f"mouse_button={button!r} has no faithful Axstream replay action")
                if count not in (1, 2):
                    raise CaptureCompileError(
                        f"click_count={count} has no faithful Axstream replay action")
                if button == "right" and count != 1:
                    raise CaptureCompileError(
                        "right-button double-click has no faithful Axstream replay action")
                do = ("right_click" if button == "right" else
                      "double_click" if count == 2 else "click")
                actions.append({"op": "act", "do": do,
                                "target": _target(record, args)})
            elif tool == "press_key":
                actions.append({"op": "act", "do": "key",
                                "keys": _keys(str(args.get("key") or ""))})
            elif tool == "type_text":
                actions.append({"op": "act", "do": "type",
                                "text": str(args.get("text") or "")})
            elif tool == "scroll":
                pages = args.get("pages", 1)
                clicks = max(1, int(pages)) if isinstance(pages, (int, float)) else 1
                actions.append({"op": "act", "do": "scroll",
                                "direction": str(args.get("direction") or "down"),
                                "clicks": clicks})
            elif tool == "drag":
                actions.append({
                    "op": "act", "do": "drag",
                    "from": {"win": _captured_window_point(
                        record, args.get("from_x"), args.get("from_y"))},
                    "to": {"win": _captured_window_point(
                        record, args.get("to_x"), args.get("to_y"))},
                })
            elif tool == "set_value":
                # Axstream's portable spelling: focus the stable element,
                # select its existing value, then type the replacement.
                actions.extend([
                    {"op": "act", "do": "click", "target": _target(record, args)},
                    {"op": "act", "do": "key", "keys": ["cmd", "a"]},
                    {"op": "act", "do": "type",
                     "text": str(args.get("value") or "")},
                ])
            elif tool == "perform_secondary_action":
                secondary = str(args.get("action") or "").casefold()
                if secondary in {"press", "open"}:
                    actions.append({"op": "act", "do": "click",
                                    "target": _target(record, args)})
                elif secondary == "show menu":
                    actions.append({"op": "act", "do": "right_click",
                                    "target": _target(record, args)})
                else:
                    raise CaptureCompileError(
                        f"secondary action {args.get('action')!r} has no "
                        "faithful Axstream replay action")
            else:
                raise CaptureCompileError(
                    f"unsupported native tool {tool!r}; keep this step in "
                    "Codex computer use until Axstream has an equivalent op")
        except CaptureCompileError as exc:
            raise CaptureCompileError(f"record {position}: {exc}") from exc

    if not actions:
        raise CaptureCompileError("capture contains no successful native actions")
    if terminal_assert is not None:
        assertion = {"op": "assert", "target": terminal_assert}
        ok, error = validate_op(assertion)
        if not ok:
            raise CaptureCompileError(f"invalid terminal assert: {error}")
        actions.append(assertion)
    actions.append({"op": "done", "status": "success"})

    slot_header, replacements, observed_hashes = _slot_specs(slots)
    if replacements:
        actions = _replace_strings(actions, replacements)

    capture_id = str(header.get("capture_id") or "")
    macro_name = _slug(name or str(header.get("name") or capture_id or "capture"))
    final_description = description or str(header.get("description") or "")
    final_when = when_to_use or str(header.get("when_to_use") or "")
    # Seed one spoken example so the menu-bar voice matcher can pick the
    # macro up immediately — the fine-tuned matcher never selects
    # example-less templates. Authors should replace this with 2-4 real
    # phrasings (slot values verbatim in the utterance).
    slot_values = {n: str((spec or {}).get("example") or n)
                   for n, spec in slot_header.items()}
    hint = (final_when or final_description or macro_name).lower().rstrip(".")
    seed_utterance = " ".join([hint] + [slot_values[n] for n in sorted(slot_values)])
    return MacroFile(
        name=macro_name,
        actions=actions,
        description=final_description,
        when_to_use=final_when,
        slots=slot_header,
        examples=[{"utterance": seed_utterance, "slots": slot_values}],
        provenance={
            "source": "codex-computer-use-trace",
            "capture_id": capture_id,
            "captured_slot_hashes": observed_hashes,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def compile_capture(capture_id: str, **kwargs: Any) -> MacroFile:
    path = resolve_capture(capture_id)
    header, records = read_capture(path)
    return compile_records(header, records, **kwargs)
