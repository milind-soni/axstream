"""OCR text anchors — the middle rung between AX elements and blind pixels.

When a click target's label exists on screen but not in the (truncated or
title-less) AX tree — Notes' toolbar "New Note" is the canonical case — Apple's
on-device Vision framework can find the rendered text in the window screenshot
in tens of milliseconds. The hit's center is clicked in SCREENSHOT-PIXEL space:
the same PNG that get_window_state produced defines the driver's window-local
pixel-click contract, so an OCR hit needs no coordinate conversion at all, and
unlike a recorded-coordinate click it is VERIFIED — the text was just seen at
that spot.

Vision is reached through pyobjc (`pip install axstream`), keeping the
repo pure Python — no Swift helper, no build step. Import is lazy and failure
is soft: without pyobjc the rung reports itself unavailable and replay falls
through to the pixel rung exactly as before.

Recognition runs .fast first (built for live camera streams; small UI chrome
text is usually enough for it) and retries once with .accurate (~0.5s) on a
miss. Vision leaks a few MB per request in long-lived processes (Apple forums,
confirmed 2025) — fine here, replay is a short-lived CLI process.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

_VISION = None  # None = untried, False = unavailable, module = ready


def _vision():
    global _VISION
    if _VISION is None:
        try:
            import Vision  # type: ignore

            _VISION = Vision
        except ImportError:
            _VISION = False
    return _VISION


def available() -> bool:
    return bool(_vision())


@dataclass
class TextHit:
    """A located piece of text, in top-left-origin image PIXELS — the exact
    space of the driver's window-local pixel-click contract."""

    x: float  # center
    y: float  # center
    text: str
    confidence: float
    level: str  # "fast" | "accurate"


def png_size(path: str) -> Optional[tuple[int, int]]:
    """Width/height straight from the PNG IHDR — no image library needed."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h) if w and h else None


def _normalize(s: str) -> str:
    return " ".join(s.split()).casefold()


def find_text(png_path: str, query: str) -> Optional[TextHit]:
    """Locate `query` in the screenshot at `png_path`. Fast pass first,
    accurate on a miss. None when Vision is unavailable, the file is not a
    readable PNG, or the text is genuinely not found."""
    Vision = _vision()
    if not Vision:
        return None
    size = png_size(png_path)
    if size is None or not _normalize(query):
        return None
    for level_name, level in (("fast", 1), ("accurate", 0)):
        hit = _find_at_level(Vision, png_path, query, size, level, level_name)
        if hit is not None:
            return hit
    return None


def all_text(png_path: str) -> list[TextHit]:
    """Every recognized text line in the screenshot (fast pass only), as
    TextHits at the line centers. Powers bubble-cursor snapping."""
    Vision = _vision()
    if not Vision:
        return []
    size = png_size(png_path)
    if size is None:
        return []
    request = _run_request(Vision, png_path, level=1)
    if request is None:
        return []
    out: list[TextHit] = []
    for obs in request.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        box = obs.boundingBox()
        out.append(TextHit(
            x=(box.origin.x + box.size.width / 2) * size[0],
            y=(1.0 - box.origin.y - box.size.height / 2) * size[1],
            text=str(cands[0].string()),
            confidence=float(cands[0].confidence()),
            level="fast"))
    return out


def nearest_text(png_path: str, x: float, y: float,
                 max_dist: float) -> Optional[TextHit]:
    """Bubble-cursor snap: the text line whose center is nearest to (x, y),
    in image pixels — but only when it is BOTH within `max_dist` and
    unambiguous (the runner-up is at least 1.5x farther away). An
    approximate click near two candidates must stay where it was rather
    than guess; a wrong snap is worse than an honest near-miss."""
    hits = all_text(png_path)
    if not hits:
        return None
    ranked = sorted(hits, key=lambda h: (h.x - x) ** 2 + (h.y - y) ** 2)
    best = ranked[0]
    d0 = ((best.x - x) ** 2 + (best.y - y) ** 2) ** 0.5
    if d0 > max_dist:
        return None
    if len(ranked) > 1:
        d1 = (((ranked[1].x - x) ** 2 + (ranked[1].y - y) ** 2) ** 0.5)
        if d1 < d0 * 1.5:
            return None
    return best


def _run_request(Vision, png_path: str, level: int):
    """Run one VNRecognizeTextRequest over the PNG; None on handler failure."""
    from Foundation import NSURL  # type: ignore

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(png_path), None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(level)
    request.setUsesLanguageCorrection_(False)
    ok, _err = handler.performRequests_error_([request], None)
    return request if ok else None


def _find_at_level(Vision, png_path: str, query: str, size: tuple[int, int],
                   level: int, level_name: str) -> Optional[TextHit]:
    from Foundation import NSMakeRange  # type: ignore

    request = _run_request(Vision, png_path, level)
    if request is None:
        return None

    q = _normalize(query)
    best: Optional[tuple[int, float, TextHit]] = None  # (rank, confidence, hit)
    for obs in request.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        line = str(cand.string())
        norm = _normalize(line)
        if norm == q:
            rank, box = 2, obs.boundingBox()
        elif q in norm:
            # sub-box of just the query inside a longer line ("New Note ⌘N"),
            # so the click lands on the words, not the line's center
            rank = 1
            idx = norm.index(q)
            box = obs.boundingBox()
            try:
                sub, _e = cand.boundingBoxForRange_error_(
                    NSMakeRange(idx, len(q)), None)
                if sub is not None:
                    box = sub.boundingBox()
            except Exception:  # noqa: BLE001 - range math on the candidate
                pass  # string can fail on ligatures; the line box still clicks
        else:
            continue
        conf = float(cand.confidence())
        # normalized, BOTTOM-left origin -> top-left-origin pixel center
        cx = (box.origin.x + box.size.width / 2) * size[0]
        cy = (1.0 - box.origin.y - box.size.height / 2) * size[1]
        hit = TextHit(x=cx, y=cy, text=line, confidence=conf, level=level_name)
        key = (rank, conf)
        if best is None or key > (best[0], best[1]):
            best = (rank, conf, hit)
    return best[2] if best else None
