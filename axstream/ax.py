"""Accessibility-tree flattening, summarization, and late-bound target resolution.

Consumes the desktop state returned by cua computer-server's
get_accessibility_tree (see cua/libs/python/computer-server/
computer_server/handlers/macos.py: get_desktop_state / UIElement.to_dict).

Element dicts carry: role, name (server replaces spaces with underscores),
description, role_description, value, enabled, absolute_position "x;y",
size "w;h", children.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

INTERACTABLE_ROLES = {
    "AXButton",
    "AXTextField",
    "AXTextArea",
    "AXSearchField",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXComboBox",
    "AXMenuItem",
    "AXMenuBarItem",
    "AXMenuButton",
    "AXLink",
    "AXTab",
    "AXSlider",
    "AXIncrementor",
    "AXDisclosureTriangle",
    "AXCell",
    "AXRow",
}


@dataclass
class AxElement:
    id: str
    role: str
    title: str
    description: str
    value: str
    center: Optional[tuple[float, float]]
    window: str


def _parse_point(s: Any) -> Optional[tuple[float, float]]:
    if not isinstance(s, str) or ";" not in s:
        return None
    try:
        x, y = s.split(";")
        return float(x), float(y)
    except ValueError:
        return None


def _center(el: dict) -> Optional[tuple[float, float]]:
    # menubar/dock items carry a bounds dict instead of UIElement position strings
    bounds = el.get("bounds")
    if isinstance(bounds, dict) and "x" in bounds:
        return bounds["x"] + bounds.get("width", 0) / 2, bounds["y"] + bounds.get("height", 0) / 2
    pos = _parse_point(el.get("absolute_position"))
    size = _parse_point(el.get("size"))
    if pos is None:
        return None
    if size is None:
        return pos
    return pos[0] + size[0] / 2, pos[1] + size[1] / 2


def _text(v: Any) -> str:
    if v is None:
        return ""
    return str(v).replace("_", " ").strip()


def match_score(ax: dict, role: str, title: str, description: str = "",
                value: str = "") -> int:
    """Fuzzy score of one candidate element against an {role?, title?} ax
    target; 0 = no match. The shared core behind Snapshot.resolve_element
    (computer-server trees) and resolve_window_element (cua-driver
    get_window_state elements), so both resolution paths rank identically:
    exact title +4, substring in title/description/value +2, +1 for a role
    constraint that matched."""
    q_role = ax.get("role")
    q_title = (ax.get("title") or "").lower()
    score = 0
    if q_role:
        if role != q_role:
            return 0
        score += 1
    if q_title:
        if q_title == title.lower():
            score += 4
        elif q_title in f"{title} {description} {value}".lower():
            score += 2
        else:
            return 0
    return score


def resolve_window_element(ax: dict, elements: list[dict]) -> Optional[dict]:
    """Fuzzy-resolve an ax target against the structured `elements` array of
    a cua-driver get_window_state snapshot ({element_index, role, label,
    value, frame, ...}). Returns the best-scoring element dict — carrying the
    element_index to click — or None. Same scoring as
    Snapshot.resolve_element."""
    best_score = 0
    best: Optional[dict] = None
    for el in elements:
        if not isinstance(el, dict) or el.get("element_index") is None:
            continue
        score = match_score(ax, el.get("role") or "", el.get("label") or "",
                            "", str(el.get("value") or ""))
        if score > best_score:
            best_score, best = score, el
    return best


class Snapshot:
    """A flattened observation of the desktop, with stable per-burst element ids."""

    def __init__(self, desktop_state: dict):
        self.elements: list[AxElement] = []
        self._by_id: dict[str, AxElement] = {}
        for win in desktop_state.get("windows", []):
            win_title = _text(win.get("title") or win.get("name")) or "window"
            for child in win.get("children", []):
                self._walk(child, win_title)
        for item in desktop_state.get("menubar_items", []):
            self._add_flat(item, "AXMenuBarItem", "menubar")
        for item in desktop_state.get("dock_items", []):
            self._add_flat(item, "AXDockItem", "dock")

    def _add_flat(self, item: Any, default_role: str, window: str) -> None:
        """Menubar/dock items: flat dicts with title + bounds, no children."""
        if not isinstance(item, dict):
            return
        center = _center(item)
        if center is None:
            return
        ax_el = AxElement(
            id=f"e{len(self.elements)}",
            role=item.get("role") or default_role,
            title=_text(item.get("title")),
            description=_text(item.get("description")),
            value="",
            center=center,
            window=window,
        )
        self.elements.append(ax_el)
        self._by_id[ax_el.id] = ax_el

    def _walk(self, el: Any, window: str) -> None:
        if not isinstance(el, dict):
            return
        role = el.get("role") or ""
        title = _text(el.get("name"))
        desc = _text(el.get("description"))
        value = _text(el.get("value"))
        keep = role in INTERACTABLE_ROLES or (
            role == "AXStaticText" and (title or value)
        )
        if keep and el.get("enabled", True):
            center = _center(el)
            if center is not None:
                ax_el = AxElement(
                    id=f"e{len(self.elements)}",
                    role=role,
                    title=title or desc,
                    description=desc,
                    value=value,
                    center=center,
                    window=window,
                )
                self.elements.append(ax_el)
                self._by_id[ax_el.id] = ax_el
        for child in el.get("children", []) or []:
            self._walk(child, window)

    def summarize(self, max_elements: int = 300) -> str:
        """Terse text rendering for the LLM prompt: one element per line."""
        lines: list[str] = []
        current_window = None
        for el in self.elements[:max_elements]:
            if el.window != current_window:
                current_window = el.window
                lines.append(f"# {current_window}")
            label = el.title or el.description or el.value or "?"
            extra = f" value={el.value!r}" if el.value and el.value != label else ""
            lines.append(f"{el.id} {el.role} {label!r}{extra}")
        if len(self.elements) > max_elements:
            lines.append(f"... {len(self.elements) - max_elements} more elements omitted")
        return "\n".join(lines)

    def resolve(self, ax: dict) -> Optional[tuple[float, float]]:
        """Resolve an ax target to screen coordinates. Returns None if no match."""
        el = self.resolve_element(ax)
        return el.center if el else None

    def resolve_element(self, ax: dict) -> Optional[AxElement]:
        if ax.get("id"):
            return self._by_id.get(ax["id"])
        best: tuple[int, Optional[AxElement]] = (0, None)
        for el in self.elements:
            score = match_score(ax, el.role, el.title, el.description, el.value)
            if score > best[0]:
                best = (score, el)
        return best[1]
