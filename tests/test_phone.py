"""phone.py — the iPhone Mirroring preflight. Pins the connection contract:
ensure_ready's ready-verdict cache (a burst of taps must not each pay the
screenshot+OCR interstitial scan) and front-only-when-needed."""

import asyncio

import pytest

from axstream import phone
from axstream.driver import DriverComputer

MIRROR = {"window_id": 9, "pid": 55, "app_name": "iPhone Mirroring",
          "title": "iPhone", "is_on_screen": True, "z_index": 5,
          "bounds": {"x": 200, "y": 100, "width": 300, "height": 650}}


class FakeMirror(DriverComputer):
    """Driver-shaped fake serving one mirror window; get_window_state claims
    a 600x1200 screenshot (the file never exists — OCR reads nothing, which
    the blocked-marker scan treats as 'no markers seen')."""

    def __init__(self, windows=None):
        super().__init__()
        self.windows = [MIRROR] if windows is None else windows
        self.calls: list[str] = []

    async def tool(self, name, /, **args):
        self.calls.append(name)
        if name == "list_windows":
            return {"windows": self.windows}
        if name == "get_window_state":
            return {"elements": [], "screenshot_width": 600,
                    "screenshot_height": 1200}
        return {}


@pytest.fixture
def mirror(monkeypatch):
    monkeypatch.setattr(phone, "_app_running", lambda: True)
    return FakeMirror()


def test_state_ready(mirror):
    s = asyncio.run(phone.state(mirror))
    assert s["state"] == "ready"
    assert s["snapshot"].pid == 55
    assert "ocr_available" in s


def test_state_not_running(monkeypatch):
    monkeypatch.setattr(phone, "_app_running", lambda: False)
    s = asyncio.run(phone.state(FakeMirror()))
    assert s["state"] == "not-running"
    assert "open the iPhone Mirroring app" in s["instructions"]


def test_state_no_window(monkeypatch):
    monkeypatch.setattr(phone, "_app_running", lambda: True)
    s = asyncio.run(phone.state(FakeMirror(windows=[])))
    assert s["state"] == "no-window"


def test_ensure_ready_caches_the_verdict(mirror):
    asyncio.run(phone.ensure_ready(mirror))
    calls_after_first = list(mirror.calls)
    asyncio.run(phone.ensure_ready(mirror))
    # the second call within the TTL skips the interstitial scan entirely:
    # no fresh get_window_state, and no re-fronting (already frontmost)
    assert "get_window_state" not in mirror.calls[len(calls_after_first):]
    assert "bring_to_front" not in mirror.calls


def test_ensure_ready_fronts_only_when_needed(mirror):
    other = {**MIRROR, "window_id": 3, "pid": 77, "app_name": "Safari",
             "z_index": 10}
    mirror.windows = [MIRROR, other]
    asyncio.run(phone.ensure_ready(mirror))
    assert mirror.calls.count("bring_to_front") == 1
    assert mirror.target_pid == 55


def test_ensure_ready_raises_with_relayable_instructions(monkeypatch):
    monkeypatch.setattr(phone, "_app_running", lambda: False)
    computer = FakeMirror()
    with pytest.raises(phone.PhoneNotReady) as exc:
        asyncio.run(phone.ensure_ready(computer))
    assert exc.value.state == "not-running"
    assert computer._phone_ready is None  # a failure poisons the cache


def test_ready_cache_expires(mirror, monkeypatch):
    asyncio.run(phone.ensure_ready(mirror))
    ts, pid = mirror._phone_ready
    mirror._phone_ready = (ts - phone._READY_TTL_S - 1, pid)
    asyncio.run(phone.ensure_ready(mirror))
    assert mirror.calls.count("get_window_state") == 2  # full preflight again
