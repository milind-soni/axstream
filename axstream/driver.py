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
DRIVER_SOCK = os.environ.get(
    "AXSTREAM_DRIVER_SOCK",
    os.path.expanduser("~/Library/Caches/cua-driver/cua-driver.sock"))

# windows owned by these never count as "the frontmost app"
_OVERLAY_APPS = {"Cua Driver", "CursorUIViewService", "Window Server", ""}


class DriverError(RuntimeError):
    pass


class DriverComputer:
    def __init__(self, binary: str = DRIVER_BIN, socket_path: str = DRIVER_SOCK):
        self.binary = binary
        self.socket_path = socket_path
        self.target_pid: Optional[int] = None  # follows `open`
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        """Hold ONE connection to the daemon's raw socket — a tool call costs
        ~0.2ms of transport vs ~100-150ms of `cua-driver call` process spawn.
        Falls back to the subprocess path when the socket isn't there."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self.socket_path, limit=16 * 1024 * 1024)  # window states are big
        except OSError:
            self._reader = self._writer = None

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._reader = self._writer = None

    async def tool(self, _tool_name: str, /, **args: Any) -> dict:
        if self._writer is not None:
            try:
                return await self._tool_socket(_tool_name, args)
            except (OSError, asyncio.IncompleteReadError, ValueError):
                # ValueError covers a desynced stream (oversized frame left a
                # fragment behind) — drop the connection, never parse garbage
                await self.close()
                await self.connect()  # one reconnect, then give it one more go
                if self._writer is not None:
                    return await self._tool_socket(_tool_name, args)
        return await self._tool_subprocess(_tool_name, args)

    async def _tool_socket(self, name: str, args: dict) -> dict:
        payload = json.dumps({"method": "call", "name": name, "args": args})
        self._writer.write(payload.encode() + b"\n")
        await self._writer.drain()
        line = await asyncio.wait_for(self._reader.readline(), timeout=30)
        if not line:
            raise OSError("driver socket closed")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise DriverError(f"{name}: {resp.get('error')}")
        result = resp.get("result") or {}
        text = " ".join(b.get("text", "") for b in result.get("content", [])
                        if isinstance(b, dict)).strip()
        if result.get("isError"):
            raise DriverError(f"{name}: {text or 'driver error'}")
        sc = result.get("structuredContent")
        out = dict(sc) if isinstance(sc, dict) else {}
        if text and "text" not in out:
            out["text"] = text
        return out

    async def _tool_subprocess(self, name: str, args: dict) -> dict:
        proc = await asyncio.create_subprocess_exec(
            self.binary, "call", name, json.dumps(args),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise DriverError(f"{name}: timed out after 30s")
        text = out.decode().strip()
        if proc.returncode != 0:
            raise DriverError(f"{name}: {err.decode().strip() or text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    async def frontmost(self) -> tuple[Optional[int], Optional[str]]:
        """(pid, app_name) of the frontmost real app — one ~5ms list_windows
        call (list_apps scans the disk and costs 1.2s+; never on a hot path)."""
        wins = await self.tool("list_windows")
        cand = [w for w in wins.get("windows", [])
                if w.get("is_on_screen")
                and (w.get("app_name") or "") not in _OVERLAY_APPS]
        if not cand:
            return None, None
        top = max(cand, key=lambda w: w.get("z_index", 0))
        return top.get("pid"), top.get("app_name")

    # -- Executor-facing surface ------------------------------------------

    async def open(self, target: str) -> None:
        is_url = "://" in target or target.startswith("www.")
        if not is_url:
            # hot path (~5ms): app already has a window on screen
            try:
                wins = (await self.tool("list_windows")).get("windows", [])
                mine = [w for w in wins if w.get("is_on_screen")
                        and (w.get("app_name") or "").lower() == target.lower()]
                if mine:
                    self.target_pid = mine[0]["pid"]
                    others = [w for w in wins if w.get("is_on_screen")
                              and (w.get("app_name") or "") not in _OVERLAY_APPS]
                    top = max(others, key=lambda w: w.get("z_index", 0)) if others else None
                    if top is not None and top.get("pid") == self.target_pid:
                        return  # already frontmost: zero further work
                    await self.tool("bring_to_front", pid=self.target_pid)
                    await asyncio.sleep(0.15)
                    return
                # running without a window (Finder et al) — slow check, cold path
                apps = await self.tool("list_apps")
                running = [a for a in apps.get("apps", [])
                           if a.get("running") and a.get("pid")
                           and (a.get("name") or "").lower() == target.lower()]
                if running:
                    self.target_pid = running[0]["pid"]
                    await self.tool("bring_to_front", pid=self.target_pid)
                    await asyncio.sleep(0.2)
                    return
            except DriverError:
                pass  # fall through to a normal launch
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
            pid, _ = await self.frontmost()
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
            pid, _ = await self.frontmost()
            if pid is None:
                raise DriverError("no target app — nothing frontmost and no `open` ran")
            self.target_pid = pid
        try:
            return await self.tool(tool_name, pid=self.target_pid, **args)
        except DriverError:
            self.target_pid = None
            raise
