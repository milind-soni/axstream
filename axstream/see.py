"""see — the screen-as-text perception layer.

One fresh window capture plus on-device OCR, shared by the MCP accelerator
tools (screen_text / find / check). This is what replaces the expensive leg
of an agent's perceive→reason→act loop: the window as TEXT with pixel
coordinates in ~200ms, instead of a screenshot plus vision reasoning.

OCR coordinates are in the window screenshot's pixel space — the exact space
an `act` click target re-verifies against, so a `find` result flows straight
into a click without conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ocr
from .driver import DriverComputer, WindowSnapshot, _usable_window


class SeeError(RuntimeError):
    """A perception failure with an agent-relayable reason."""


@dataclass
class WindowView:
    """One fresh window capture and every text line recognized in it."""

    snapshot: WindowSnapshot
    hits: list  # of ocr.TextHit

    @property
    def title(self) -> str:
        return self.snapshot.title

    @property
    def pid(self) -> int:
        return self.snapshot.pid

    @property
    def screenshot_size(self) -> Optional[tuple[int, int]]:
        return self.snapshot.screenshot_size


async def targeted_computer(app: Optional[str]) -> DriverComputer:
    """A connected DriverComputer, pinned to `app`'s window when given."""
    c = DriverComputer()
    await c.connect()
    if app:
        wins = (await c.tool("list_windows")).get("windows", [])
        mine = [w for w in wins
                if (w.get("app_name") or "").lower() == app.lower()
                and _usable_window(w)]
        if not mine:
            await c.close()
            raise SeeError(f"no window found for app {app!r}")
        c.target_pid = mine[0]["pid"]
    return c


async def capture(computer: DriverComputer) -> WindowSnapshot:
    """A fresh window screenshot, or a SeeError naming what went wrong."""
    g = await computer.window_geometry(fresh_shot=True)
    if g is None or not g.fresh_shot or not g.shot_path:
        raise SeeError("could not capture the target window")
    return g


async def window_view(computer: DriverComputer) -> WindowView:
    """Fresh capture + every OCR'd text line in it."""
    snap = await capture(computer)
    return WindowView(snapshot=snap, hits=ocr.all_text(snap.shot_path))
