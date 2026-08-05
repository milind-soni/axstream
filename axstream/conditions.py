"""Adaptive UI conditions shared by streamed execution and macro replay."""

from __future__ import annotations

import asyncio
import json
import time

from . import ocr
from .ax import Snapshot, resolve_window_element


class ConditionTimeout(RuntimeError):
    pass


async def wait_for_target(computer, target: dict, *, timeout_ms: int = 2500,
                          poll_ms: int = 120) -> dict:
    """Poll for an AX element or rendered text and return resolution evidence.

    The first observation happens immediately.  Unlike a fixed ``wait`` this
    therefore adds almost no latency when the UI is already ready, while still
    tolerating slow pages and cold app transitions up to ``timeout_ms``.
    """
    timeout_ms = max(0, int(timeout_ms))
    poll_ms = max(20, int(poll_ms))
    deadline = time.perf_counter() + timeout_ms / 1000
    text = target.get("text") if isinstance(target.get("text"), str) else ""
    ax = target.get("ax") if isinstance(target.get("ax"), dict) else None

    if not text.strip() and not ax:
        raise ConditionTimeout(
            f"condition target has neither text nor ax: {json.dumps(target)}")

    while True:
        if text.strip():
            if not ocr.available():
                raise ConditionTimeout(
                    "OCR unavailable — install the macOS OCR dependency")
            geometry = await computer.window_geometry(fresh_shot=True)
            if geometry is not None and geometry.fresh_shot and geometry.shot_path:
                hit = ocr.find_text(geometry.shot_path, text)
                if hit is not None:
                    return {
                        "via": "ocr",
                        "resolved": f"text {hit.text!r} visible [{hit.level} ocr]",
                        "window": geometry.title,
                    }
        elif ax is not None:
            if hasattr(computer, "window_snapshot"):
                snapshot = await computer.window_snapshot(with_screenshot=False)
                if snapshot is not None:
                    element = resolve_window_element(ax, snapshot.elements)
                    if element is not None:
                        return {
                            "via": "ax",
                            "resolved": (f"{element.get('role', '')} "
                                         f"{element.get('label', '')!r}"),
                            "window": snapshot.title,
                        }
            else:
                state = await computer.ax_tree()
                element = Snapshot(state).resolve_element(ax)
                if element is not None:
                    return {"via": "ax",
                            "resolved": f"{element.role} {element.title!r}"}

        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_ms / 1000, remaining))

    if text.strip():
        raise ConditionTimeout(
            f"text {text!r} not visible within {timeout_ms}ms")
    raise ConditionTimeout(
        f"target did not resolve within {timeout_ms}ms: {json.dumps(target)}")
