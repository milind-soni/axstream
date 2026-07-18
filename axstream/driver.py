"""cua-driver execution backend — the reliable executor edge.

Talks to the `cua-driver` binary (MCP over stdio, newline-delimited JSON) and
exposes the same method surface the Executor calls on a Computer. Unlike the
computer-server WebSocket path, cua-driver delivers keyboard/mouse to a
specific pid in the background (no focus race, no full-desktop AX-walk hang),
so replay is reliable.

`open` records the launched app's pid; subsequent key/type/scroll go to that
pid (mirrors the Swift app's targetPid). Coordinate clicks use desktop scope.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

DRIVER_BIN = os.path.expanduser("~/.local/bin/cua-driver")


class DriverError(RuntimeError):
    pass


class DriverComputer:
    def __init__(self, binary: str = DRIVER_BIN):
        self.binary = binary
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._id = 0
        self._lock = asyncio.Lock()
        self.target_pid: Optional[int] = None  # follows `open`

    async def connect(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.binary, "mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "axstream", "version": "0.1"},
        })
        await self._notify("notifications/initialized")

    async def close(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()
            self._proc = None

    # -- MCP plumbing ------------------------------------------------------

    async def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _rpc(self, method: str, params: dict) -> dict:
        assert self._proc and self._proc.stdout
        self._id += 1
        rid = self._id
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)
            if not line:
                raise DriverError("cua-driver closed the stream")
            msg = json.loads(line)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise DriverError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def tool(self, _tool_name: str, /, **args: Any) -> dict:
        """Call an MCP tool; returns its structured content (parsed)."""
        async with self._lock:
            result = await self._rpc("tools/call", {"name": _tool_name, "arguments": args})
        content = result.get("content") or []
        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, KeyError):
                    return {"text": block.get("text", "")}
        return result.get("structuredContent") or {}

    # -- Executor-facing surface ------------------------------------------

    async def open(self, target: str) -> None:
        is_url = "://" in target or target.startswith("www.")
        if is_url:
            url = target if "://" in target else f"https://{target}"
            res = await self.tool("launch_app", name="Safari", urls=[url])
        else:
            res = await self.tool("launch_app", name=target)
        pid = self._extract_pid(res)
        if pid:
            self.target_pid = pid
            await self.tool("bring_to_front", pid=pid)
        await asyncio.sleep(0.4)  # app settle

    @staticmethod
    def _extract_pid(res: dict) -> Optional[int]:
        """launch_app reports the pid either as a field or in prose text
        ('Launched TextEdit (pid 6821) ...')."""
        if isinstance(res.get("pid"), int):
            return res["pid"]
        import re

        m = re.search(r"pid[:\s]+(\d+)", res.get("text", ""))
        return int(m.group(1)) if m else None

    async def type_text(self, text: str) -> None:
        await self.tool("type_text", pid=self._pid(), text=text)

    async def key(self, keys: list[str]) -> None:
        if len(keys) == 1:
            await self.tool("press_key", pid=self._pid(), key=keys[0])
        else:
            await self.tool("hotkey", pid=self._pid(), keys=keys)

    async def scroll(self, direction: str, clicks: int = 1) -> None:
        await self.tool("scroll", pid=self._pid(), direction=direction,
                        amount=max(1, min(50, clicks)))

    async def click(self, x: float, y: float) -> None:
        await self.tool("click", x=int(x), y=int(y), scope="desktop")

    async def double_click(self, x: float, y: float) -> None:
        await self.tool("double_click", pid=self._pid(), x=int(x), y=int(y))

    async def move(self, x: float, y: float) -> None:
        await self.tool("move_cursor", x=int(x), y=int(y))

    def _pid(self) -> int:
        if self.target_pid is None:
            raise DriverError("no target app — an `open` must run before key/type")
        return self.target_pid
