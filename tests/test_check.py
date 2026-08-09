"""check.py — the verify/wait layer. Regression coverage for the scroll and
settle family (a module merge once shipped them with missing imports; these
tests exercise the real code paths end to end)."""

import asyncio

from axstream.check import scroll_screen, scroll_until, wait_stable
from axstream.driver import WindowSnapshot


class FakeGeo:
    """Geometry-serving fake: one static window screenshot, scrolls logged."""

    def __init__(self, shot_path):
        self.shot_path = str(shot_path)
        self.scrolls = 0

    async def window_geometry(self, fresh_shot=False):
        return WindowSnapshot(pid=1, window_id=1, title="w",
                              screenshot_size=(10, 10),
                              shot_path=self.shot_path, fresh_shot=True)

    async def scroll(self, direction, clicks=1):
        self.scrolls += 1


def test_wait_stable_identical_captures(tmp_path):
    shot = tmp_path / "w.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    computer = FakeGeo(shot)
    assert asyncio.run(wait_stable(computer, timeout=2, interval=0.01)) is True


def test_scroll_screen_runs_default_content_path(tmp_path):
    # The default content filter + settle loop must not crash even when OCR
    # reads nothing (no Vision, or an unreadable PNG): the screen reports
    # "moved" on zero overlap and the caller decides what that means.
    shot = tmp_path / "w.png"
    shot.write_bytes(b"not a png")
    computer = FakeGeo(shot)
    res = asyncio.run(scroll_screen(computer, settle=0.01))
    assert set(res) == {"moved", "overlap", "hits"}
    assert computer.scrolls == 1


def test_scroll_until_stops_at_max_scrolls(tmp_path):
    shot = tmp_path / "w.png"
    shot.write_bytes(b"not a png")
    computer = FakeGeo(shot)
    done_seen = []
    res = asyncio.run(scroll_until(
        computer, lambda hits: done_seen.append(len(hits)) or None,
        max_scrolls=2, settle=0.01))
    assert res is None
    assert computer.scrolls == 2
