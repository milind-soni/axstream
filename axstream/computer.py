"""Thin client for cua's computer-server over one persistent WebSocket.

Protocol (cua/libs/python/computer-server/computer_server/main.py):
  send {"command": <name>, "params": {...}} -> recv {"success": bool, ...}
Local servers (no CONTAINER_NAME env) require no auth handshake.

This deliberately bypasses cua's Python `computer` client, which opens a new
HTTP session per command; here every command reuses one socket.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import websockets


class ComputerError(RuntimeError):
    pass


class Computer:
    def __init__(self, uri: str = "ws://localhost:8000/ws"):
        self.uri = uri
        self._ws: Optional[websockets.ClientConnection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.uri, max_size=None)

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def command(self, command: str, **params: Any) -> dict:
        if self._ws is None:
            await self.connect()
        assert self._ws is not None
        async with self._lock:
            await self._ws.send(json.dumps({"command": command, "params": params}))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
        result = json.loads(raw)
        if not result.get("success", True):
            raise ComputerError(f"{command}: {result.get('error', 'unknown error')}")
        return result

    # -- actions used by the executor ------------------------------------

    async def click(self, x: float, y: float) -> None:
        await self.command("left_click", x=int(x), y=int(y))

    async def double_click(self, x: float, y: float) -> None:
        await self.command("double_click", x=int(x), y=int(y))

    async def move(self, x: float, y: float) -> None:
        await self.command("move_cursor", x=int(x), y=int(y))

    async def type_text(self, text: str) -> None:
        await self.command("type_text", text=text)

    async def key(self, keys: list[str]) -> None:
        if len(keys) == 1:
            await self.command("press_key", key=keys[0])
        else:
            await self.command("hotkey", keys=keys)

    async def scroll(self, direction: str, clicks: int = 1) -> None:
        await self.command("scroll_direction", direction=direction, clicks=clicks)

    async def open(self, target: str) -> None:
        # `open` handles apps, files, and URLs uniformly on macOS.
        arg = f"-a '{target}'" if not ("://" in target or "/" in target or "." in target) else f"'{target}'"
        await self.command("run_command", command=f"open {arg}")

    async def ax_tree(self) -> dict:
        return await self.command("get_accessibility_tree")

    async def screenshot(self) -> dict:
        return await self.command("screenshot")


class MockComputer(Computer):
    """Dry-run backend: logs commands with timestamps instead of executing.

    `latency` simulates per-command execution time so the overlap timeline in
    demos is realistic.
    """

    def __init__(self, latency: float = 0.03, ax_fixture: Optional[dict] = None):
        super().__init__(uri="mock://")
        self.latency = latency
        self.ax_fixture = ax_fixture or {"windows": [], "menubar_items": [], "dock_items": []}
        self.log: list[tuple[float, str, dict]] = []

    async def connect(self) -> None:  # pragma: no cover - nothing to connect
        pass

    async def command(self, command: str, **params: Any) -> dict:
        latency = self.latency
        if command == "type_text":
            latency += 0.03 * len(params.get("text", ""))  # ~real keystroke pacing
        await asyncio.sleep(latency)
        self.log.append((time.perf_counter(), command, params))
        if command == "get_accessibility_tree":
            return {"success": True, **self.ax_fixture}
        return {"success": True}
