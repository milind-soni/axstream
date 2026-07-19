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
import re
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
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise DriverError(f"{_tool_name}: timed out after 30s")
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
        if pid is None:
            # no pid = the launch didn't land (unknown app name, driver error).
            # Fail the action so replay aborts and the caller can fall back —
            # a silent no-op reported as success is worse than a slow retry.
            raise DriverError(f"open {target!r}: launch_app returned no pid ({res})")
        self.target_pid = pid
        await self.tool("bring_to_front", pid=pid)
        # condition wait, not a fixed sleep: an on-screen window means the app
        # is ready for keys; cold launches (Firefox) can take seconds
        for _ in range(25):
            if await self._front_window(pid) is not None:
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)  # brief settle after the window appears

    async def _front_window(self, pid: int) -> Optional[dict]:
        wins = await self.tool("list_windows")
        mine = [w for w in wins.get("windows", [])
                if w.get("pid") == pid and w.get("is_on_screen") and w.get("title") is not None]
        return max(mine, key=lambda w: w.get("z_index", 0)) if mine else None

    async def ax_tree(self, frontmost_only: bool = True, max_depth: int = 20) -> dict:
        """Observation, mapped to the computer-server desktop-state shape that
        Snapshot consumes. Observes the tracked pid (after an `open`) or the
        frontmost app; get_window_state frames are already screen-global."""
        pid = self.target_pid
        if pid is None:
            apps = await self.tool("list_apps")
            active = [a for a in apps.get("apps", [])
                      if a.get("active") and a.get("running")]
            pid = active[0]["pid"] if active else None
        empty = {"windows": [], "menubar_items": [], "dock_items": []}
        if pid is None:
            return empty
        win = await self._front_window(pid)
        if win is None:
            return empty
        self.target_pid = pid  # keys/type follow the observed app
        state = await self.tool("get_window_state", pid=pid,
                                window_id=win["window_id"],
                                include_screenshot=False, max_elements=500)
        children = []
        for el in state.get("elements", []):
            if el.get("role") == "AXWindow":
                continue
            f = el.get("frame") or {}
            children.append({
                "role": el.get("role", ""),
                "name": el.get("label") or "",
                "value": "" if el.get("value") is None else str(el.get("value")),
                "enabled": True,
                "absolute_position": f"{f.get('x', 0)};{f.get('y', 0)}",
                "size": f"{f.get('w', 0)};{f.get('h', 0)}",
                "children": [],
            })
        title = win.get("title") or "window"
        return {"windows": [{"title": title, "children": children}],
                "menubar_items": [], "dock_items": []}

    @staticmethod
    def _extract_pid(res: dict) -> Optional[int]:
        """launch_app reports the pid as a field, in prose text
        ('Launched TextEdit (pid 6821) ...'), or inside MCP content blocks."""
        if isinstance(res.get("pid"), int):
            return res["pid"]
        texts = [res.get("text", "")]
        for block in res.get("content", []) or []:
            if isinstance(block, dict):
                texts.append(str(block.get("text", "")))
        for t in texts:
            m = re.search(r"pid[:\s]+(\d+)", t)
            if m:
                return int(m.group(1))
        return None

    # spec/computer-server key names -> driver vocabulary
    _KEYMAP = {"enter": "return", "esc": "escape", "arrowup": "up",
               "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
               "command": "cmd", "alt": "option", "backspace": "delete"}

    async def type_text(self, text: str) -> None:
        await self._pid_tool("type_text", text=text)

    async def key(self, keys: list[str]) -> None:
        keys = [self._KEYMAP.get(k.lower(), k) for k in keys]
        if len(keys) == 1:
            await self._pid_tool("press_key", key=keys[0])
        else:
            await self._pid_tool("hotkey", keys=keys)

    async def scroll(self, direction: str, clicks: int = 1) -> None:
        await self._pid_tool("scroll", direction=direction,
                             amount=max(1, min(50, clicks)))

    async def click(self, x: float, y: float) -> None:
        await self.tool("click", x=int(x), y=int(y), scope="desktop")

    async def double_click(self, x: float, y: float) -> None:
        # x/y are SCREEN coords on the driver's pixel path (unlike click, a
        # pid is required here, but it does not make the coords window-local)
        await self._pid_tool("double_click", x=int(x), y=int(y))

    async def move(self, x: float, y: float) -> None:
        # moves the driver's overlay cursor, not the real pointer — visual only
        await self.tool("move_cursor", x=int(x), y=int(y))

    async def _pid_tool(self, tool_name: str, **args: Any) -> dict:
        """A tool call targeting the tracked pid; a failure drops the pid so
        the next action fails fast at `open` instead of deep in a replay.
        With no tracked pid, targets the frontmost app — this is what makes
        context-free macros ("copy that", "select all") act on whatever app
        the user is in."""
        if self.target_pid is None:
            apps = await self.tool("list_apps")
            active = [a for a in apps.get("apps", [])
                      if a.get("active") and a.get("running")]
            if not active:
                raise DriverError("no target app — nothing frontmost and no `open` ran")
            self.target_pid = active[0]["pid"]
        try:
            return await self.tool(tool_name, pid=self.target_pid, **args)
        except DriverError:
            self.target_pid = None
            raise
