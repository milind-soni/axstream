"""Screen-settle and list-walking primitives (ported from phone-harness).

Works on any window target, not just the iPhone mirror — but it exists
because the mirror has no DOM: the capture is the only ground truth, so
"did the screen move" and "has it stopped changing" must be answered from
pixels and OCR.

The load-bearing idea: end-of-list is decided by whether the SCREEN MOVED,
never by whether a parser found new items. A dense screen or a missed OCR
row must not read as "done" — only the pixels going still (after a settle
window that lets lazy-loaded content arrive) means the end.

Everything here captures with fresh_shot=True: the driver's shot cache
serves stale pixels on a size-keyed hit, which would defeat every
comparison this module makes.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .driver import DriverComputer, WindowSnapshot
from . import ocr as _ocr

# Region of the window (height fractions) whose OCR participates in movement
# detection. Callers with chrome to exclude (the mirror's status bar) pass
# their own; the defaults take the whole window.
ContentFilter = Callable[[WindowSnapshot], list]


def _default_content(snap: WindowSnapshot) -> list:
    if not snap.shot_path:
        return []
    return _ocr.all_text(snap.shot_path)


def _text_set(hits: list) -> frozenset:
    return frozenset(h.text.strip() for h in hits if h.text.strip())


def _overlap(a: frozenset, b: frozenset) -> float:
    """Jaccard overlap of two text sets: ~1.0 = same screen, low = it moved."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def _fresh(computer: DriverComputer) -> Optional[WindowSnapshot]:
    return await computer.window_geometry(fresh_shot=True)


async def wait_stable(computer: DriverComputer, timeout: float = 6.0,
                      interval: float = 0.5, settle: int = 2) -> bool:
    """True once `settle` consecutive captures are byte-identical (animation
    finished). A clock in window chrome ticks at most once a minute, so
    near-misses are rare; callers with volatile chrome should still prefer
    scroll_screen's OCR-overlap movement test for scrolling decisions."""
    prev, same = None, 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = await _fresh(computer)
        if snap is None or not snap.shot_path:
            return False
        digest = hashlib.md5(Path(snap.shot_path).read_bytes()).hexdigest()
        same = same + 1 if digest == prev else 0
        if same >= settle - 1:
            return True
        prev = digest
        await asyncio.sleep(interval)
    return False


async def _settled_content(computer: DriverComputer, content: ContentFilter,
                           settle: float) -> tuple[frozenset, list]:
    """Poll content OCR until two consecutive reads agree (or the settle
    window runs out) — lazy-loaded rows arrive before movement is judged."""
    prev_set: Optional[frozenset] = None
    prev_hits: list = []
    deadline = time.time() + settle
    while time.time() < deadline:
        snap = await _fresh(computer)
        hits = content(snap) if snap else []
        cur = _text_set(hits)
        if cur == prev_set:
            break
        prev_set, prev_hits = cur, hits
        await asyncio.sleep(0.35)
    return prev_set or frozenset(), prev_hits


async def scroll_screen(computer: DriverComputer, direction: str = "up",
                        clicks: int = 4, settle: float = 2.5,
                        moved_thresh: float = 0.6,
                        content: ContentFilter = _default_content) -> dict:
    """One scroll, then wait for the screen to settle and judge movement.

    Returns {moved, overlap, hits} — `hits` is the settled content OCR ready
    to parse. `moved` is False when overlap >= moved_thresh. The 0.6 default
    sits in the measured gap between real forward progress (overlap < ~0.45)
    and overscroll bounce at a boundary (overlap > ~0.7), which springs the
    content back and would otherwise defeat end-detection.

    Direction follows axstream's scroll op: 'up' reveals content below
    (wheel-up = content advances), matching how lists are read to the end.
    """
    before, _ = await _settled_content(computer, content, settle=0.01)
    await computer.scroll(direction, clicks=clicks)
    await asyncio.sleep(0.4)
    after, hits = await _settled_content(computer, content, settle)
    ov = _overlap(before, after)
    return {"moved": ov < moved_thresh, "overlap": round(ov, 3), "hits": hits}


async def scroll_until(computer: DriverComputer,
                       done: Callable[[list], object],
                       direction: str = "up", clicks: int = 4,
                       max_scrolls: int = 60, settle: float = 2.5,
                       content: ContentFilter = _default_content) -> object:
    """Scroll until `done(hits)` is truthy or the list stops moving.
    Returns done's truthy value, or None when the end came first."""
    snap = await _fresh(computer)
    hit = done(content(snap) if snap else [])
    if hit:
        return hit
    stale = 0
    for _ in range(max_scrolls):
        res = await scroll_screen(computer, direction, clicks, settle,
                                  content=content)
        hit = done(res["hits"])
        if hit:
            return hit
        if res["moved"]:
            stale = 0
        else:
            stale += 1
            if stale >= 2:  # confirmed still after a retry
                return None
            await asyncio.sleep(0.8)
    return None


async def scroll_collect(computer: DriverComputer,
                         extract: Optional[Callable[[list], list]] = None,
                         key: Optional[Callable[[object], object]] = None,
                         direction: str = "up", clicks: int = 4,
                         max_scrolls: int = 400, end_after: int = 3,
                         settle: float = 2.5,
                         content: ContentFilter = _default_content,
                         on_progress: Optional[Callable] = None) -> dict:
    """Walk a list top-to-bottom, extracting and de-duping items each screen,
    until it reaches its true end.

    - extract(hits) -> items for the current screen (default: text lines).
    - key(item) -> hashable de-dup key (default: the item itself).
    - Stops after `end_after` consecutive non-moving scrolls, or max_scrolls.

    Returns {items, stop, scrolls}; stop is 'reached-end' or 'max-scrolls'.
    Keep clicks modest so consecutive screens overlap and no row falls
    between captures.
    """
    extract = extract or (lambda hits: [h.text.strip() for h in hits
                                        if h.text.strip()])
    key = key or (lambda item: item)
    seen: set = set()
    order: list = []

    def ingest(hits: list) -> int:
        new = 0
        for item in extract(hits):
            k = key(item)
            if k in seen:
                continue
            seen.add(k)
            order.append(item)
            new += 1
        return new

    snap = await _fresh(computer)
    ingest(content(snap) if snap else [])
    stale = 0
    for i in range(1, max_scrolls + 1):
        res = await scroll_screen(computer, direction, clicks, settle,
                                  content=content)
        new = ingest(res["hits"])
        if on_progress:
            on_progress(i, len(order), new, res["moved"], res["overlap"])
        if res["moved"]:
            stale = 0
        else:
            stale += 1
            if stale >= end_after:
                return {"items": order, "stop": "reached-end", "scrolls": i}
            await asyncio.sleep(0.8)  # extra grace for a slow lazy-load
    return {"items": order, "stop": "max-scrolls", "scrolls": max_scrolls}
