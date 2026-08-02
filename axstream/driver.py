"""cua-driver execution backend — the reliable executor edge.

Talks to the `cua-driver` binary (MCP over stdio, newline-delimited JSON) and
exposes the same method surface the Executor calls on a Computer. Unlike the
computer-server WebSocket path, cua-driver delivers keyboard/mouse to a
specific pid in the background (no focus race, no full-desktop AX-walk hang),
so replay is reliable.

`open` records the launched app's pid; subsequent key/type/scroll go to that
pid (mirrors the Swift app's targetPid). File-macro replay clicks go through
`window_snapshot` + `click_element`/`click_window_pixel` (element-addressed AX
actions, or pid-addressed window-local pixels — never desktop scope); only the
streaming/burst tier still uses the raw desktop-scope `click`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

DRIVER_BIN = os.path.expanduser("~/.local/bin/cua-driver")
def _default_socket() -> str:
    """Socket discovery order: explicit env override -> an axstream-dedicated
    daemon (a locally patched driver, e.g. running the observe_window_changes
    fast path before it ships upstream) -> the standard cua-driver daemon."""
    env = os.environ.get("AXSTREAM_DRIVER_SOCK")
    if env:
        return env
    dedicated = os.path.expanduser(
        "~/Library/Caches/cua-driver/axstream-driver.sock")
    if os.path.exists(dedicated):
        return dedicated
    return os.path.expanduser("~/Library/Caches/cua-driver/cua-driver.sock")


DRIVER_SOCK = _default_socket()

# windows owned by these never count as "the frontmost app"
_OVERLAY_APPS = {"Cua Driver", "CursorUIViewService", "Window Server", ""}

# axstream's own agent-cursor instance. Clicks pass it as `session` so the
# fast motion profile below applies to OUR overlay cursor only — other driver
# clients (SupaMaus, agents) keep their default human-paced glide. UNIQUE per
# DriverComputer INSTANCE, not per process: the driver tracks session
# lifecycle and eventually marks idle sessions "ended", which REJECTS tool
# calls until revived — fatal for a long-lived MCP server process that
# reuses one process-wide id across hours (observed live twice). A fresh id
# per instance is auto-created by the daemon on first use.
import uuid as _uuid


class DriverError(RuntimeError):
    pass


@dataclass
class WindowSnapshot:
    """One get_window_state observation of the target app's front window.

    `elements` is the driver's structured array ({element_index, role, label,
    value, frame, ...}); pass an element_index back to click/double_click for
    the AX-action path. `bounds` is the window frame from list_windows —
    LOGICAL points, top-left origin, screen-global. `screenshot_size` is the
    (w, h) of this snapshot's window screenshot — the pixel space the
    driver's window-local pixel-click contract expects — present only when
    the snapshot was taken with_screenshot=True."""

    pid: int
    window_id: int
    title: str
    elements: list[dict] = field(default_factory=list)
    bounds: dict = field(default_factory=dict)
    screenshot_size: Optional[tuple[int, int]] = None
    # path of this snapshot's window screenshot PNG, when one was captured —
    # the OCR text-anchor rung reads it. Only trustworthy as CURRENT pixels
    # when the snapshot was freshly captured (window_geometry(fresh_shot=True));
    # a cache-served snapshot's file may show the window as it looked earlier.
    shot_path: Optional[str] = None
    fresh_shot: bool = False


def window_pixels_from_screen(gx: float, gy: float, bounds: dict,
                              screenshot_size: tuple[int, int]) -> tuple[float, float]:
    """Global logical screen coords -> window-local screenshot pixels.

    The driver's pixel-click contract (click.rs / double_click.rs, when
    pid+window_id are passed): x/y are pixels of the get_window_state window
    screenshot, top-left origin. Internally the driver multiplies by any
    registered snapshot-downscale ratio, divides by the backing scale it
    detects (screenshot px width / logical window width — Retina 2x), and
    adds the window's screen origin. This is the exact inverse: subtract the
    origin, multiply by screenshot-size / logical-bounds. Using the
    dimensions of OUR OWN latest snapshot keeps the downscale ratio the
    driver registered consistent with this math."""
    w = bounds.get("width") or 0
    h = bounds.get("height") or 0
    sx = (screenshot_size[0] / w) if w else 1.0
    sy = (screenshot_size[1] / h) if h else 1.0
    return (gx - bounds.get("x", 0)) * sx, (gy - bounds.get("y", 0)) * sy


class DriverComputer:
    def __init__(self, binary: str = DRIVER_BIN, socket_path: str = DRIVER_SOCK):
        self.binary = binary
        self.socket_path = socket_path
        self.target_pid: Optional[int] = None  # follows `open`
        # "foreground" = escalated input delivery for apps that drop
        # background pid-addressed events (Blender-class OpenGL/GHOST UIs):
        # the driver briefly fronts the target window, posts at the HID/menu
        # level, and restores the prior app. Set from a macro's header
        # ("delivery": "foreground"); None = polite background delivery.
        self.delivery: Optional[str] = None
        self._cursor_session = f"axstream-{_uuid.uuid4().hex[:8]}"
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        # last known screenshot size per (pid, window_id), with the window
        # SIZE it was captured at — the screenshot's pixel dimensions (and the
        # downscale ratio the driver registers per pid) depend only on the
        # window's logical size, so cached dims stay valid while the window
        # merely MOVES; a resize invalidates them. Also serves window captures
        # that fail transiently right after a click (observed live on Notes).
        # value: (logical (w, h), screenshot (w, h) px, screenshot file path)
        self._shot_cache: dict[tuple[int, int],
                               tuple[tuple, tuple[int, int], str]] = {}

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

    # tool calls that are safe to re-send after a socket hiccup. Input
    # actions are NOT here on purpose: if the first send executed but the
    # response read failed, a blind retry DOUBLE-EXECUTES it — observed live
    # as a URL typed twice into Safari (type_text retried after a timeout).
    # An idempotent read fails soft; a doubled click/keystroke corrupts state.
    _IDEMPOTENT = {"list_windows", "list_apps", "get_window_state",
                   "get_screen_size", "get_desktop_state", "check_permissions",
                   "health_report", "get_config", "get_accessibility_tree",
                   "set_agent_cursor_motion", "bring_to_front"}

    async def tool(self, _tool_name: str, /, **args: Any) -> dict:
        if self._writer is not None:
            try:
                return await self._tool_socket(_tool_name, args)
            except (OSError, asyncio.IncompleteReadError, ValueError) as e:
                # ValueError covers a desynced stream (oversized frame left a
                # fragment behind) — drop the connection, never parse garbage
                await self.close()
                await self.connect()  # reconnect for subsequent calls
                if _tool_name in self._IDEMPOTENT and self._writer is not None:
                    return await self._tool_socket(_tool_name, args)
                if _tool_name not in self._IDEMPOTENT:
                    raise DriverError(
                        f"{_tool_name}: driver connection failed mid-call ({e}) "
                        "— NOT retried (the action may already have executed; "
                        "a blind retry could double-act). Check the app state "
                        "before re-issuing.") from e
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
                    # condition wait: the window may live on another Space and
                    # takes a beat to arrive on screen after bring_to_front
                    if await self._await_window(self.target_pid, tries=10) is not None:
                        await asyncio.sleep(0.2)
                        return
                    # window never arrived (off-Space + focus contention) —
                    # fall through to launch_app, which forces a real
                    # activation / Space switch (observed live: bring_to_front
                    # alone left the Notes window off screen)
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
        await self._await_window(pid)
        await asyncio.sleep(0.2)  # brief settle after the window appears

    async def _await_window(self, pid: int, tries: int = 25) -> Optional[dict]:
        """Condition-wait for an on-screen titled window of `pid` (Space
        switches and cold launches take a beat). Returns it, or None."""
        for _ in range(tries):
            win = await self._front_window(pid)
            if win is not None:
                return win
            await asyncio.sleep(0.2)
        return None

    async def _front_window(self, pid: int) -> Optional[dict]:
        # non-EMPTY title required: transient tooltip/hover windows (Notes
        # spawns 1512x33 title-'' strips ABOVE the main window in z) must
        # never be picked as "the" window — observed live breaking replay's
        # per-click snapshots.
        # on-screen preferred, NOT required: when the user holds focus on
        # another Space (observed live — activation refused while they work),
        # the app's window stays off screen, but AX walks, element actions,
        # and pid-addressed events all still work there — replay proceeds in
        # the background instead of failing with "no window"
        wins = await self.tool("list_windows")
        mine = [w for w in wins.get("windows", [])
                if w.get("pid") == pid and w.get("title")]
        on_screen = [w for w in mine if w.get("is_on_screen")]
        pick = on_screen or mine
        return max(pick, key=lambda w: w.get("z_index", 0)) if pick else None

    async def window_snapshot(self, with_screenshot: bool = False) -> Optional[WindowSnapshot]:
        """Fresh get_window_state of the tracked (or frontmost) app's front
        window. Take one before EVERY element-addressed action: the driver's
        element_index cache is scoped per snapshot and replaced by the next
        one (click.rs: "re-snapshot every turn"). with_screenshot=True also
        captures the window screenshot — to a temp file, purely to learn its
        pixel dimensions (and let the driver register a downscale ratio
        consistent with them) for window-local pixel-fallback clicks."""
        pid = self.target_pid
        if pid is None:
            pid, _ = await self.frontmost()
        if pid is None:
            return None
        win = await self._front_window(pid)
        if win is None:
            return None
        self.target_pid = pid  # keys/type follow the observed app
        # max_elements is a LATENCY dial, not just a context one: measured on
        # Notes (2.5k-note sidebar) a 2000-element walk costs ~19s per click vs
        # ~3s at 500. Keep it low. The cost of a low cap is that a depth-first
        # walk can truncate before reaching a toolbar — handled by targeting
        # such elements by recorded coordinates rather than by raising this.
        args: dict[str, Any] = dict(pid=pid, window_id=win["window_id"],
                                    include_screenshot=False, max_elements=500)
        shot_path = None
        if with_screenshot:
            shot_path = self._shot_file()
            args["screenshot_out_file"] = shot_path
        state = await self.tool("get_window_state", **args)
        size = None
        sw, sh = state.get("screenshot_width"), state.get("screenshot_height")
        if isinstance(sw, int) and isinstance(sh, int) and sw > 0 and sh > 0:
            size = (sw, sh)
        if with_screenshot:
            size = self._cache_shot(pid, win, size, shot_path)
        return WindowSnapshot(pid=pid, window_id=win["window_id"],
                              title=win.get("title") or "window",
                              elements=state.get("elements") or [],
                              bounds=win.get("bounds") or {},
                              screenshot_size=size,
                              shot_path=shot_path if size is not None else None,
                              fresh_shot=with_screenshot and size is not None)

    async def window_geometry(self, fresh_shot: bool = False) -> Optional[WindowSnapshot]:
        """Cheap geometry for pixel-addressed clicks: live window bounds plus
        the screenshot pixel dimensions, WITHOUT the element walk. A pixel
        click needs no element_index, and get_window_state's AX walk is the
        entire per-click cost (measured on Notes: ~1s at max_elements=500 vs
        126-206ms at max_elements=1 with the screenshot, ~14ms for the
        list_windows bounds alone).

        Serving dims from the size-keyed shot cache makes repeat clicks on an
        unmoved-size window cost one list_windows call. The max_elements=1
        get_window_state still runs on a cache MISS so the driver's per-pid
        downscale registration stays consistent with the dims we compute
        against — that registration only changes when the window size does.

        fresh_shot=True forces a new capture even on a cache hit: the OCR
        text-anchor rung needs the window's CURRENT pixels, not just its
        dimensions."""
        pid = self.target_pid
        if pid is None:
            pid, _ = await self.frontmost()
        if pid is None:
            return None
        win = await self._front_window(pid)
        if win is None:
            return None
        self.target_pid = pid
        bounds = win.get("bounds") or {}
        key = (pid, win["window_id"])
        skey = (bounds.get("width"), bounds.get("height"))
        cached = self._shot_cache.get(key)
        if not fresh_shot and cached is not None and cached[0] == skey:
            return WindowSnapshot(pid=pid, window_id=win["window_id"],
                                  title=win.get("title") or "window",
                                  bounds=bounds, screenshot_size=cached[1],
                                  shot_path=cached[2])
        shot_path = self._shot_file()
        state = await self.tool("get_window_state", pid=pid,
                                window_id=win["window_id"],
                                include_screenshot=False, max_elements=1,
                                screenshot_out_file=shot_path)
        size = None
        sw, sh = state.get("screenshot_width"), state.get("screenshot_height")
        if isinstance(sw, int) and isinstance(sh, int) and sw > 0 and sh > 0:
            size = (sw, sh)
        got_fresh = size is not None
        size = self._cache_shot(pid, win, size, shot_path)
        return WindowSnapshot(pid=pid, window_id=win["window_id"],
                              title=win.get("title") or "window",
                              bounds=bounds, screenshot_size=size,
                              shot_path=shot_path if size is not None else None,
                              fresh_shot=got_fresh)

    def _shot_file(self) -> str:
        return os.path.join(tempfile.gettempdir(),
                            f"axstream-snap-{os.getpid()}.png")

    def _cache_shot(self, pid: int, win: dict, size: Optional[tuple[int, int]],
                    shot_path: Optional[str]) -> Optional[tuple[int, int]]:
        """Record a capture's dims under the window's logical size — or, when
        the capture hiccuped (transient post-click failures observed live on
        Notes), serve the cached dims as long as the size is unchanged."""
        key = (pid, win["window_id"])
        bounds = win.get("bounds") or {}
        skey = (bounds.get("width"), bounds.get("height"))
        if size is not None:
            self._shot_cache[key] = (skey, size, shot_path or self._shot_file())
            return size
        cached = self._shot_cache.get(key)
        if cached is not None and cached[0] == skey:
            return cached[1]
        return None

    # -- element-addressed + window-local actions (the replay click ladder) --
    # Each returns the driver's structured result (click.rs echoes
    # {"path": "ax"|"cgevent"..., "effect": ...}) so callers can surface
    # WHICH delivery path actually ran.

    async def fast_cursor(self) -> None:
        """Give axstream's cursor instance a near-instant motion profile.
        The driver's default speed-based glide (~900pt/s peak, plus an 80ms
        post-click dwell) costs ~1-1.3s of pure animation per click —
        measured live as the dominant per-click latency once the element
        walk was gone. Replay calls this once per run; failure is soft (an
        older driver without the tool just keeps the pretty glide)."""
        try:
            await self.tool("set_agent_cursor_motion",
                            cursor_id=self._cursor_session, glide_duration_ms=50,
                            dwell_after_click_ms=0, spring=1.0)
        except DriverError:
            pass

    async def click_element(self, pid: int, window_id: int, element_index: int) -> dict:
        """AXUIElementPerformAction on a cached element — no cursor move, no
        focus steal, works on background windows. element_index must come
        from the latest window_snapshot of this (pid, window_id)."""
        return await self.tool("click", pid=pid, window_id=window_id,
                               element_index=element_index,
                               session=self._cursor_session,
                               **self._replay_args())

    async def double_click_element(self, pid: int, window_id: int, element_index: int) -> dict:
        """AX double-click: the driver performs AXOpen when advertised, else
        a pixel double-click at the element's center (double_click.rs)."""
        return await self.tool("double_click", pid=pid, window_id=window_id,
                               element_index=element_index,
                               session=self._cursor_session,
                               **self._replay_args())

    async def click_window_pixel(self, pid: int, window_id: int, x: float, y: float) -> dict:
        """Pid-addressed CGEvent click; x/y are WINDOW-LOCAL screenshot
        pixels (see window_pixels_from_screen). Never desktop scope."""
        return await self.tool("click", pid=pid, window_id=window_id,
                               x=round(x, 2), y=round(y, 2),
                               session=self._cursor_session,
                               **self._replay_args())

    async def double_click_window_pixel(self, pid: int, window_id: int, x: float, y: float) -> dict:
        return await self.tool("double_click", pid=pid, window_id=window_id,
                               x=round(x, 2), y=round(y, 2),
                               session=self._cursor_session,
                               **self._replay_args())

    def _replay_args(self) -> dict:
        """Per-call extras for deterministic replay: opt out of the driver's
        up-to-1s post-action window-change poll (a replay knows what comes
        next — patched drivers honor it, older ones ignore the unknown arg),
        and escalate delivery when the macro declares it."""
        out: dict = {"observe_window_changes": False}
        if self.delivery == "foreground":
            out["delivery_mode"] = "foreground"
        return out

    async def ax_tree(self, frontmost_only: bool = True, max_depth: int = 20) -> dict:
        """Observation, mapped to the computer-server desktop-state shape that
        Snapshot consumes. Observes the tracked pid (after an `open`) or the
        frontmost app; get_window_state frames are already screen-global."""
        empty = {"windows": [], "menubar_items": [], "dock_items": []}
        snap = await self.window_snapshot()
        if snap is None:
            return empty
        children = []
        for el in snap.elements:
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
        return {"windows": [{"title": snap.title, "children": children}],
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
        # RAW desktop-pixel CGEvent click at the global HID tap — steals the
        # real pointer. Only the streaming/burst tier still routes here; file
        # replay (replay.py) resolves clicks through the element/window-local
        # ladder above and must NEVER reach this.
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
        extra = self._replay_args()
        if (extra.pop("delivery_mode", None) == "foreground"
                and tool_name in ("press_key", "hotkey", "type_text")):
            # Foreground delivery = the macro OWNS the foreground for its run:
            # front the app once, then post keys at the global HID tap
            # (scope:"desktop", no pid) — the only input Blender-class
            # OpenGL/GHOST apps accept. pid-addressed posts are ignored by
            # them even when frontmost (measured live).
            if not getattr(self, "_fronted", False):
                await self.tool("bring_to_front", pid=self.target_pid)
                await asyncio.sleep(0.3)
                self._fronted = True
            args["scope"] = "desktop"
            return await self.tool(tool_name, **args, **extra)
        try:
            return await self.tool(tool_name, pid=self.target_pid, **args, **extra)
        except DriverError:
            self.target_pid = None
            raise
