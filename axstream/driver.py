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
        self.target_pid: Optional[int] = None  # follows `open`

    async def connect(self) -> None:
        """No persistent process needed — `cua-driver call` proxies to the
        always-running CuaDriver daemon. Kept for interface parity."""
        return None

    async def close(self) -> None:
        return None

    async def tool(self, _tool_name: str, /, **args: Any) -> dict:
        """Call a driver tool via `cua-driver call` (proxies to the warm
        daemon in ~10ms). Faster and simpler than the persistent MCP-stdio
        path, which added ~1s/call overhead."""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "call", _tool_name, json.dumps(args),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        text = out.decode().strip()
        if proc.returncode != 0:
            raise DriverError(f"{_tool_name}: {err.decode().strip() or text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

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
