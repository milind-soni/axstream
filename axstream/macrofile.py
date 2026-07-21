"""File-based macros: one ``.axstream`` file = one replayable skill.

The format is agent-first — a coding agent (Claude Code) can author, diff,
and refine a macro as a plain text file, then run it with ``axstream replay``.

Layout of a ``.axstream`` file:

  line 1 (optional)  a JSON *header* object — any JSON object WITHOUT an
                     ``"op"`` key. Fields: name, description, when_to_use,
                     slots {name: {description, example}}, provenance
                     {source, capture_id?, created}, examples (optional,
                     matcher-style phrasings for the frecency store bridge).
  remaining lines    axstream-spec 0.1 ops, one JSON object per line
                     (see SPEC.md). Blank lines and ``#`` comments are
                     ignored. Slot placeholders use the SAME templating the
                     macro store already uses: ``{slot_name}`` inside string
                     arguments (macros._fill).

A *raw draft* — e.g. a SupaMaus recording export — is the same file with the
header optional (or a provenance-only header). Clicks in drafts may carry BOTH
coordinates and the clicked element's AX label in one target:

  {"op":"act","do":"click","target":{"x":420,"y":312,"ax":{"title":"Save"}}}

Replay resolves the AX label against the live tree first and falls back to
the coordinates (see replay.py).

The header may alternatively live in a sidecar ``<name>.json`` next to a
header-less ``.axstream`` file.

Macros are discovered in ``./.axstream/macros/*.axstream`` (project) then
``~/.axstream/macros/`` (user).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .macros import Macro, _fill
from .spec import validate_op

MACRO_SUFFIX = ".axstream"
PROJECT_DIR = Path(".axstream") / "macros"
USER_DIR = Path("~/.axstream/macros")

# header keys we understand; unknown keys are preserved in `extra`
_HEADER_KEYS = {"name", "description", "when_to_use", "slots", "provenance", "examples"}

_SLOT_RE = re.compile(r"\{(\w+)\}")


class MacroFileError(ValueError):
    """A macro file that cannot be loaded, validated, or slot-filled."""


@dataclass
class MacroFile:
    name: str
    actions: list[dict]
    description: str = ""
    when_to_use: str = ""
    slots: dict[str, dict] = field(default_factory=dict)  # {name: {description, example}}
    provenance: dict = field(default_factory=dict)  # {source, capture_id?, created}
    examples: list[dict] = field(default_factory=list)  # optional matcher examples
    extra: dict = field(default_factory=dict)  # unknown header fields, preserved
    path: Optional[Path] = None

    # -- slots ------------------------------------------------------------

    def used_slots(self) -> set[str]:
        """Every {placeholder} referenced anywhere in the action args."""
        found: set[str] = set()

        def walk(v: Any) -> None:
            if isinstance(v, str):
                found.update(_SLOT_RE.findall(v))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(self.actions)
        return found

    def fill(self, values: dict[str, Any]) -> list[dict]:
        """Substitute slot values into the actions. Declared slots that appear
        in the actions MUST be provided; placeholders that are NOT declared in
        the header are left verbatim (they are literal braces, not slots) —
        so a header-less draft never errors here."""
        needed = self.used_slots() & set(self.slots)
        missing = sorted(needed - set(values))
        if missing:
            raise MacroFileError(
                f"missing slot value(s): {', '.join(missing)} "
                f"(pass --slots '{json.dumps({m: '...' for m in missing})}')"
            )
        return _fill(self.actions, {k: str(v) for k, v in values.items()})


# -- parse / serialize ----------------------------------------------------


def parse(text: str, name_hint: str = "", path: Optional[Path] = None) -> MacroFile:
    """Parse macro-file text. Header optional (raw-draft case)."""
    header: Optional[dict] = None
    actions: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise MacroFileError(f"line {lineno}: not valid JSON ({e.msg})") from e
        if not isinstance(obj, dict):
            raise MacroFileError(f"line {lineno}: expected a JSON object")
        if header is None and not actions and "op" not in obj and "do" not in obj:
            header = obj  # first object without an op = the header
            continue
        ok, err = validate_op(obj)  # also normalizes {"op":"click"} shorthand
        if not ok:
            raise MacroFileError(f"line {lineno}: invalid op: {err}")
        actions.append(obj)
    if not actions:
        raise MacroFileError("no action lines found")
    return _assemble(header or {}, actions, name_hint, path)


def _assemble(header: dict, actions: list[dict], name_hint: str, path: Optional[Path]) -> MacroFile:
    slots_raw = header.get("slots") or {}
    if isinstance(slots_raw, list):  # tolerate the store's list-of-names shape
        slots = {str(s): {} for s in slots_raw}
    elif isinstance(slots_raw, dict):
        slots = {str(k): (v if isinstance(v, dict) else {"description": str(v)})
                 for k, v in slots_raw.items()}
    else:
        raise MacroFileError(f"header slots must be an object or list, got {slots_raw!r}")
    return MacroFile(
        name=str(header.get("name") or name_hint or "unnamed"),
        actions=actions,
        description=str(header.get("description") or ""),
        when_to_use=str(header.get("when_to_use") or ""),
        slots=slots,
        provenance=dict(header.get("provenance") or {}),
        examples=list(header.get("examples") or []),
        extra={k: v for k, v in header.items() if k not in _HEADER_KEYS},
        path=path,
    )


def dumps(mf: MacroFile) -> str:
    """Serialize: one compact header line, then one op per line."""
    header: dict[str, Any] = {"name": mf.name}
    if mf.description:
        header["description"] = mf.description
    if mf.when_to_use:
        header["when_to_use"] = mf.when_to_use
    if mf.slots:
        header["slots"] = mf.slots
    if mf.provenance:
        header["provenance"] = mf.provenance
    if mf.examples:
        header["examples"] = mf.examples
    header.update(mf.extra)
    lines = [json.dumps(header, ensure_ascii=False)]
    lines += [json.dumps(op, ensure_ascii=False) for op in mf.actions]
    return "\n".join(lines) + "\n"


def load(path: str | Path) -> MacroFile:
    path = Path(path).expanduser()
    try:
        text = path.read_text()
    except OSError as e:
        raise MacroFileError(f"cannot read {path}: {e}") from e
    mf = parse(text, name_hint=path.stem, path=path)
    if not mf.description and not mf.slots and not mf.provenance:
        sidecar = path.with_suffix(".json")  # header may live next to the file
        if sidecar.exists():
            try:
                header = json.loads(sidecar.read_text())
            except json.JSONDecodeError as e:
                raise MacroFileError(f"sidecar {sidecar}: not valid JSON ({e.msg})") from e
            if isinstance(header, dict):
                mf = _assemble(header, mf.actions, path.stem, path)
    return mf


def save(mf: MacroFile, path: str | Path) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(mf))
    mf.path = path
    return path


# -- discovery ------------------------------------------------------------


def macro_dirs() -> list[Path]:
    """Search order: project dir (cwd-relative) first, then the user dir."""
    return [PROJECT_DIR, USER_DIR.expanduser()]


def discover(dirs: Optional[list[Path]] = None) -> list[tuple[Path, MacroFile | MacroFileError]]:
    """Every macro file found, in search order. Broken files are returned as
    their error instead of silently skipped — agents should see them."""
    out: list[tuple[Path, MacroFile | MacroFileError]] = []
    for d in dirs if dirs is not None else macro_dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.glob(f"*{MACRO_SUFFIX}")):
            try:
                out.append((p, load(p)))
            except MacroFileError as e:
                out.append((p, e))
    return out


def resolve_name(name_or_path: str, dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """A path that exists wins; otherwise search the macro dirs by file stem,
    then by header name."""
    p = Path(name_or_path).expanduser()
    if p.is_file():
        return p
    search = dirs if dirs is not None else macro_dirs()
    for d in search:
        cand = d / f"{name_or_path}{MACRO_SUFFIX}"
        if cand.is_file():
            return cand
    for path, mf in discover(search):
        if isinstance(mf, MacroFile) and mf.name == name_or_path:
            return path
    return None


# -- frecency-store bridge -------------------------------------------------
# The JSON macro store (macros.MacroStore) stays as-is: it is the matcher's
# ranked index for the voice tier. These converters let file macros be seeded
# into it (and captured macros exported to files) without either side changing.


def to_macro(mf: MacroFile) -> Macro:
    return Macro(
        id=re.sub(r"[^a-z0-9_]+", "_", mf.name.lower()).strip("_") or "unnamed",
        description=mf.description or mf.when_to_use or mf.name,
        slots=sorted(mf.used_slots() & set(mf.slots)) or sorted(mf.slots),
        examples=mf.examples,
        actions=mf.actions,
    )


def from_macro(m: Macro, provenance: Optional[dict] = None) -> MacroFile:
    return MacroFile(
        name=m.id,
        actions=m.actions,
        description=m.description,
        slots={s: {} for s in m.slots},
        examples=m.examples,
        provenance=provenance or {"source": "llm-run"},
    )
