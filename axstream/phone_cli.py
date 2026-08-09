"""`axstream phone` — drive the iPhone Mirroring window.

Two shapes:
  axstream phone --doctor        preflight the mirroring session
  axstream phone <<'PY'          exec Python with phone helpers pre-imported
  tap_text("Weather")
  PY

Inside a script, every phone helper is a plain (synchronous-looking) call: a
shared DriverComputer is connected once and each helper is run to completion
on it, so scripts read like phone-harness's did — no async ceremony.
"""

from __future__ import annotations

import asyncio
import sys

from .driver import DriverComputer
from . import phone as _phone
from . import check as _settle

USAGE = """Usage:
  axstream phone --doctor        preflight the iPhone Mirroring session
  axstream phone <<'PY'          run a script with phone helpers pre-imported
  print(state())
  PY

Helpers (all synchronous in a script): state, screenshot, ocr, tap, tap_text,
type_text, swipe, scroll, scroll_screen, scroll_until, scroll_collect,
wait_stable, home, app_switcher, open_app, press, wait.
"""


def cmd_phone(argv: list[str]) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    if argv and argv[0] in {"--doctor", "doctor"}:
        return asyncio.run(_doctor())
    if sys.stdin.isatty():
        print(USAGE)
        return 2
    code = sys.stdin.read()
    if not code.strip():
        print(USAGE)
        return 2
    return asyncio.run(_exec_script(code))


async def _doctor() -> int:
    computer = DriverComputer()
    try:
        await computer.connect()
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        print(f"✘ cua-driver not reachable: {exc}")
        return 1
    try:
        s = await _phone.state(computer)
    finally:
        await computer.close()
    icon = "✔" if s["state"] == "ready" else "✘"
    print(f"{icon} iPhone Mirroring: {s['state']}")
    if s["instructions"]:
        print(f"  → {s['instructions']}")
    if not s.get("ocr_available", True):
        print("  ⚠ OCR unavailable (pip install axstream) — the blocked-"
              "interstitial scan is blind; 'ready' may hide a Connect screen")
    if s["state"] == "ready":
        snap = s["snapshot"]
        print(f"  window {snap.bounds.get('width')}x{snap.bounds.get('height')} "
              f"@ ({snap.bounds.get('x')},{snap.bounds.get('y')})")
    return 0 if s["state"] == "ready" else 1


async def _exec_script(code: str) -> int:
    """Expose each async phone/settle helper as a blocking call bound to one
    connected computer, then exec the user's script in that namespace."""
    computer = DriverComputer()
    await computer.connect()
    loop = asyncio.get_running_loop()

    def sync(coro_fn):
        # The script runs in a worker thread (below); bounce each helper's
        # coroutine back onto this loop and block the worker for its result.
        def wrapper(*args, **kwargs):
            fut = asyncio.run_coroutine_threadsafe(
                coro_fn(computer, *args, **kwargs), loop)
            return fut.result()
        return wrapper

    ns = {
        "state": sync(_phone.state),
        "ensure_ready": sync(_phone.ensure_ready),
        "tap_text": sync(_phone.tap_text),
        "type_text": sync(_phone.type_text),
        "swipe": sync(_phone.swipe),
        "home": sync(_phone.home),
        "app_switcher": sync(_phone.app_switcher),
        "open_app": sync(_phone.open_app),
        "wait_stable": sync(_settle.wait_stable),
        "scroll_screen": sync(_settle.scroll_screen),
        "scroll_until": sync(_settle.scroll_until),
        "scroll_collect": sync(_settle.scroll_collect),
        "screenshot": sync(_screenshot),
        "ocr": sync(_ocr_hits),
        "tap": sync(_tap_point),
        "scroll": sync(_scroll),
        "press": sync(_press),
        "wait": _wait,
        "computer": computer,
        "__name__": "__main__",
    }
    try:
        return await loop.run_in_executor(None, _run_blocking, code, ns)
    finally:
        await computer.close()


def _run_blocking(code: str, ns: dict) -> int:
    try:
        exec(code, ns)
        return 0
    except _phone.PhoneNotReady as exc:
        print(f"phone not ready ({exc.state}): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface to the agent
        import traceback
        traceback.print_exc()
        return 1


# --- helper coroutines bound by _exec_script.sync ---

async def _screenshot(computer: DriverComputer, path: str | None = None) -> str:
    snap = await _phone.ensure_ready(computer)
    snap = await computer.window_geometry(fresh_shot=True) or snap
    return snap.shot_path or ""


async def _ocr_hits(computer: DriverComputer, min_confidence: float = 0.3) -> list:
    snap = await _phone.ensure_ready(computer)
    snap = await computer.window_geometry(fresh_shot=True) or snap
    return [{"text": h.text, "confidence": round(h.confidence, 3),
             "x": h.x, "y": h.y}
            for h in _phone._visible_text(snap) if h.confidence >= min_confidence]


async def _tap_point(computer: DriverComputer, x: float, y: float) -> None:
    snap = await _phone.ensure_ready(computer)
    await computer.click_window_pixel(snap.pid, snap.window_id, x, y)


async def _scroll(computer: DriverComputer, direction: str = "up",
                  clicks: int = 4) -> None:
    await _phone.ensure_ready(computer)
    await computer.scroll(direction, clicks=clicks)


async def _press(computer: DriverComputer, combo: str) -> None:
    await _phone.ensure_ready(computer)
    await computer.key(combo.split("+"))


def _wait(seconds: float = 1.0) -> None:
    import time
    time.sleep(seconds)
