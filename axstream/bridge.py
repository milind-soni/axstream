"""Voice bridge: local WebSocket server connecting a UI shell (the Electron
bottom bar) to the axstream pipeline.

Protocol (JSON messages):
  UI -> bridge: {"type":"start_listen"} | {"type":"stop_listen"} | {"type":"cancel"}
  bridge -> UI: {"type":"ready"}
                {"type":"listening"}
                {"type":"partial","text":...}        (live transcript while speaking)
                {"type":"transcript","text":...,"stt_ms":...}
                {"type":"event", ...runner/executor event...}
                {"type":"task_done","seconds":...}
                {"type":"error","message":...}

Run:  uv run --extra voice python -m axstream.bridge
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import websockets

from .computer import Computer
from .llm import stream_openai_compat
from .runner import run_task
from .voice import SAMPLE_RATE, load_transcriber

PARTIAL_INTERVAL = 0.6  # seconds between live re-transcriptions while speaking


def load_env_keys() -> None:
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k == "CLAUDE_API":
                    k = "ANTHROPIC_API_KEY"
                if k and v and k not in os.environ:
                    os.environ[k] = v


class Bridge:
    def __init__(self, computer_uri: str, model: str):
        self.computer = Computer(uri=computer_uri)
        self.model = model
        self.transcriber = None
        self._chunks: list = []
        self._stream = None
        self._partial_task: Optional[asyncio.Task] = None
        self._run_task: Optional[asyncio.Task] = None
        # Parakeet/MLX is not safe under concurrent transcribe calls: the live
        # partial loop and the final key-release transcribe must serialize.
        self._stt_lock = asyncio.Lock()

    async def _transcribe(self, audio) -> str:
        async with self._stt_lock:
            return await asyncio.to_thread(self.transcriber.transcribe, audio)

    def stream_factory(self, system: str, user: str):
        extra = {"reasoning_effort": "low"} if "gpt-oss" in self.model else None
        return stream_openai_compat(
            system, user, model=self.model,
            api_key=os.environ["GROQ_API_KEY"],
            base_url="https://api.groq.com/openai/v1", extra=extra,
        )

    async def prepare(self) -> None:
        import numpy as np

        self.transcriber = await asyncio.to_thread(load_transcriber)
        await asyncio.to_thread(
            self.transcriber.transcribe, np.zeros(8000, dtype="float32")
        )  # burn MLX compile
        await self.computer.connect()

    # -- recording ----------------------------------------------------------

    def _start_recording(self) -> None:
        import sounddevice as sd

        self._chunks = []
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=lambda indata, *_: self._chunks.append(indata.copy()),
        )
        self._stream.start()

    def _stop_recording(self):
        import numpy as np

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._chunks:
            return np.zeros(1, dtype="float32")
        return np.concatenate(self._chunks)[:, 0]

    def _buffer_audio(self):
        import numpy as np

        if not self._chunks:
            return None
        return np.concatenate(self._chunks)[:, 0]

    async def _partial_loop(self, send) -> None:
        last = ""
        while True:
            await asyncio.sleep(PARTIAL_INTERVAL)
            if self._stt_lock.locked():
                continue  # don't queue up behind an in-flight transcribe
            audio = self._buffer_audio()
            if audio is None or len(audio) < SAMPLE_RATE // 4:
                continue
            try:
                text = await self._transcribe(audio)
            except Exception:  # noqa: BLE001 - partials are best-effort
                continue
            if text and text != last:
                last = text
                await send({"type": "partial", "text": text})

    # -- session ------------------------------------------------------------

    async def handle(self, ws) -> None:
        async def send(obj: dict) -> None:
            try:
                await ws.send(json.dumps(obj))
            except websockets.ConnectionClosed:
                pass

        await send({"type": "ready"})
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "start_listen" and self._stream is None:
                self._start_recording()
                self._partial_task = asyncio.create_task(self._partial_loop(send))
                await send({"type": "listening"})

            elif kind == "stop_listen" and self._stream is not None:
                if self._partial_task:
                    self._partial_task.cancel()
                    self._partial_task = None
                audio = self._stop_recording()
                t0 = time.perf_counter()
                text = await self._transcribe(audio)
                stt_ms = (time.perf_counter() - t0) * 1000
                await send({"type": "transcript", "text": text, "stt_ms": stt_ms})
                if text:
                    # latest voice command wins: cancel any running task
                    if self._run_task and not self._run_task.done():
                        self._run_task.cancel()
                        await send({"type": "event", "kind": "interrupted"})
                    self._run_task = asyncio.create_task(self._run(text, send))

            elif kind == "cancel":
                if self._partial_task:
                    self._partial_task.cancel()
                    self._partial_task = None
                if self._stream is not None:
                    self._stop_recording()

    async def _run(self, task: str, send) -> None:
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def forward() -> None:
            while True:
                event = await queue.get()
                if event is None:
                    break
                await send({"type": "event", **event})

        forwarder = asyncio.create_task(forward())
        try:
            await asyncio.wait_for(
                run_task(
                    self.computer, task, self.stream_factory,
                    max_bursts=6, verbose=True, on_event=on_event,
                ),
                timeout=90,
            )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass  # interrupted by a newer command, or wedged: either way move on
        except Exception as e:  # noqa: BLE001 - surface to the UI
            await send({"type": "error", "message": str(e)})
        finally:
            queue.put_nowait(None)
            await forwarder
            await send({"type": "task_done", "seconds": time.perf_counter() - t0})


async def main() -> None:
    load_env_keys()
    model = os.environ.get("AXSTREAM_MODEL", "qwen/qwen3.6-27b")
    bridge = Bridge(
        computer_uri=os.environ.get("AXSTREAM_COMPUTER", "ws://localhost:8765/ws"),
        model=model,
    )
    print("loading STT + connecting to computer-server...")
    await bridge.prepare()
    print(f"bridge ready on ws://localhost:8790  (model {model})")
    async with websockets.serve(bridge.handle, "localhost", 8790):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
