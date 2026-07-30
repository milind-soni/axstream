"""Visual patch anchors — verified pixel clicks for what OCR cannot see.

The OCR rung (ocr.py) verifies a click only when the target has RENDERED
TEXT. Icon-only controls — toolbar glyphs, segmented controls, the Notes
"New Note" pencil — fall through to the blind window-pixel rung and count as
unverified. This module closes that gap with a template-match anchor: a tiny
grayscale crop of the control, saved into the macro at learn time, located
again in the live window screenshot at replay time. A hit is target-VERIFIED
exactly like an OCR hit — the control was just seen at that spot.

The anchor rides in the op target, self-contained (a macro file stays one
copyable file):

  {"op":"act","do":"click","target":{"x":420,"y":312,
      "patch":{"png":"<base64 PNG>","fx":0.27,"fy":0.31,"sw":1446,"sh":1300}}}

`fx`/`fy` are the recorded click as a fraction of the recorded SCREENSHOT
(`sw`x`sh` pixels — the same PNG space as the driver's window-local
pixel-click contract, zero conversion). Replay searches a small region
around the expected point only: a patch is a local verifier, not a global
finder — searching the whole window would trade a blind click for a
confident wrong one.

Matching is tried on raw grayscale first (near-1.0 when nothing changed)
and on Canny edges second: measured live (HANDOVER §4e), a dark<->light
theme flip kills raw correlation (~0.2) while edge correlation holds ~0.8
at the correct location. A known window resize rescales the patch by the
screenshot-size ratio before matching (raw patches die at ±10% scale;
rescaled ones measure ~0.98).

Capture REFUSES ambiguous controls: a lock icon repeated down a list matches
itself elsewhere with margin 0.00 — an anchor is only saved when the
best-vs-runner-up margin across the whole screenshot clears MIN_MARGIN.
Refusal is honest; a duplicate-looking anchor would replay onto the wrong
row silently.

OpenCV is reached through the optional `axstream[patch]` extra. Import is
lazy and failure is soft — without cv2 the rung reports itself unavailable
and replay falls through exactly as before (same contract as ocr.py).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

_CV2 = None  # None = untried, False = unavailable, module = ready

# raw-grayscale acceptance: essentially "nothing changed since recording"
RAW_THRESHOLD = 0.88
# edge acceptance: theme-flipped windows measured 0.78-0.88 at the true spot
EDGE_THRESHOLD = 0.75
# capture-time uniqueness: text-label patches measured margins 0.27-0.39,
# repeated icons 0.00 — refuse anything that another spot nearly ties
MIN_MARGIN = 0.12
# default crop around the click point, button-shaped (w, h), screenshot px
CROP_W, CROP_H = 140, 56
# search this many px around the expected point (grown to 2x patch size)
SEARCH_RADIUS = 96


def _cv2():
    global _CV2
    if _CV2 is None:
        try:
            import cv2  # type: ignore

            _CV2 = cv2
        except ImportError:
            _CV2 = False
    return _CV2


def available() -> bool:
    return bool(_cv2())


@dataclass
class PatchHit:
    """A re-located anchor, in top-left-origin screenshot PIXELS."""

    x: float  # center
    y: float  # center
    score: float
    method: str  # "raw" | "edge"


def valid_fragment(patch: object) -> bool:
    """Shape check for a target.patch fragment (spec.py calls this)."""
    if not isinstance(patch, dict):
        return False
    if not isinstance(patch.get("png"), str) or not patch["png"]:
        return False
    return all(isinstance(patch.get(k), (int, float)) for k in ("fx", "fy", "sw", "sh"))


def _decode(fragment: dict):
    cv2 = _cv2()
    import numpy as np

    try:
        raw = base64.b64decode(fragment["png"], validate=True)
    except (ValueError, KeyError):
        return None
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    return img if img is not None and img.size else None


def _load_gray(png_path: str):
    cv2 = _cv2()
    img = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)
    return img if img is not None and img.size else None


def _edges(img):
    return _cv2().Canny(img, 60, 160)


def _best_match(image, patch) -> Optional[tuple[float, int, int]]:
    """(score, x, y) of the best normalized match, or None if the patch
    does not fit inside the image."""
    cv2 = _cv2()
    ih, iw = image.shape[:2]
    ph, pw = patch.shape[:2]
    if ph > ih or pw > iw or ph < 4 or pw < 4:
        return None
    res = cv2.matchTemplate(image, patch, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return float(score), int(loc[0]), int(loc[1])


def find_patch(png_path: str, fragment: dict) -> Optional[PatchHit]:
    """Locate the anchor in the live screenshot at `png_path`. Searches only
    around the expected point (scaled for any screenshot-size change). None
    when cv2 is unavailable, the images are unreadable, or the control is
    genuinely not there — the caller falls through and stays honest."""
    if not available() or not valid_fragment(fragment):
        return None
    frame = _load_gray(png_path)
    patch = _decode(fragment)
    if frame is None or patch is None:
        return None
    cv2 = _cv2()

    fh, fw = frame.shape[:2]
    rw, rh = float(fragment["sw"]), float(fragment["sh"])
    if rw <= 0 or rh <= 0:
        return None
    sx, sy = fw / rw, fh / rh
    # a known size change rescales the patch (measured: raw match dies at
    # ±10% scale, a ratio-rescaled patch matches ~0.98)
    if abs(sx - 1.0) > 0.02 or abs(sy - 1.0) > 0.02:
        nw, nh = max(4, round(patch.shape[1] * sx)), max(4, round(patch.shape[0] * sy))
        patch = cv2.resize(patch, (nw, nh))

    ex, ey = fragment["fx"] * fw, fragment["fy"] * fh
    ph, pw = patch.shape[:2]
    r = max(SEARCH_RADIUS, pw * 2, ph * 2)
    x0, y0 = max(0, int(ex - r - pw / 2)), max(0, int(ey - r - ph / 2))
    x1, y1 = min(fw, int(ex + r + pw / 2)), min(fh, int(ey + r + ph / 2))
    region = frame[y0:y1, x0:x1]

    for method, img, pat, threshold in (
        ("raw", region, patch, RAW_THRESHOLD),
        ("edge", _edges(region), _edges(patch), EDGE_THRESHOLD),
    ):
        m = _best_match(img, pat)
        if m is None:
            return None  # patch larger than the search window — unusable
        score, mx, my = m
        if score >= threshold:
            return PatchHit(x=x0 + mx + pw / 2, y=y0 + my + ph / 2,
                            score=score, method=method)
    return None


def capture_patch(png_path: str, x: float, y: float) -> Optional[dict]:
    """Cut an anchor around screenshot-pixel point (x, y) and return the
    target.patch fragment — or None when the crop is not UNIQUE enough in
    its own screenshot to anchor on (repeated icons, blank chrome). The
    uniqueness gate is the load-bearing part; see the module docstring."""
    if not available():
        return None
    frame = _load_gray(png_path)
    if frame is None:
        return None
    cv2 = _cv2()

    fh, fw = frame.shape[:2]
    if not (0 <= x < fw and 0 <= y < fh):
        return None
    x0, y0 = max(0, int(x - CROP_W / 2)), max(0, int(y - CROP_H / 2))
    x1, y1 = min(fw, x0 + CROP_W), min(fh, y0 + CROP_H)
    crop = frame[y0:y1, x0:x1]
    if crop.shape[0] < 16 or crop.shape[1] < 16 or int(crop.std()) < 4:
        return None  # too small or featureless (blank chrome anchors nothing)

    m = _best_match(frame, crop)
    if m is None:
        return None
    best, bx, by = m
    if best < 0.95:
        return None
    # mask the best hit and ask what the runner-up scores — a near-tie means
    # this control looks like another one and must not become an anchor
    masked = frame.copy()
    mh, mw = crop.shape[:2]
    masked[max(0, by - mh // 2):by + mh + mh // 2,
           max(0, bx - mw // 2):bx + mw + mw // 2] = 0
    m2 = _best_match(masked, crop)
    if m2 is not None and m2[0] > best - MIN_MARGIN:
        return None

    ok, buf = cv2.imencode(".png", crop)
    if not ok:
        return None
    return {
        "png": base64.b64encode(buf.tobytes()).decode("ascii"),
        "fx": round(x / fw, 6),
        "fy": round(y / fh, 6),
        "sw": fw,
        "sh": fh,
    }
