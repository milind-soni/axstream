"""Test double for run_actions: records calls, resolves nothing."""
import asyncio


class MockComputer:
    def __init__(self, latency=0.0):
        self.latency = latency
        self.log = []

    async def _hit(self, *a):
        self.log.append(a)
        if self.latency:
            await asyncio.sleep(self.latency)

    async def connect(self): pass
    async def close(self): pass
    async def fast_cursor(self): pass
    async def open(self, target): await self._hit("open", target)
    async def type_text(self, text): await self._hit("type_text", text)
    async def key(self, keys): await self._hit("key", tuple(keys))
    async def scroll(self, d, clicks=1): await self._hit("scroll", d, clicks)
    async def move(self, x, y): await self._hit("move", x, y)
    async def window_snapshot(self, with_screenshot=False): return None
    async def window_geometry(self, fresh_shot=False): return None
