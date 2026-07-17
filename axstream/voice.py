"""Local speech-to-text front-end.

Transcriber backends (auto-selected, both fully local):
  - Parakeet TDT via parakeet-mlx (Apple Silicon / MLX) -- the fast path,
    same model family BlueyLite uses via FluidAudio/CoreML.
  - whisper.cpp via pywhispercpp -- fallback.

Recording is push-to-talk in v0: Enter starts, Enter stops. Deterministic
endpointing beats VAD on total latency (you release the key, we act) and
needs zero extra dependencies; VAD auto-endpointing can layer in later.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Protocol

SAMPLE_RATE = 16_000


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio) -> str: ...  # float32 mono @ 16kHz -> text


class ParakeetTranscriber:
    name = "parakeet-mlx"

    def __init__(self, model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        from parakeet_mlx import from_pretrained  # lazy: heavy import

        self._model = from_pretrained(model_id)

    def transcribe(self, audio) -> str:
        # parakeet-mlx's transcribe() takes a file path, so round-trip through
        # a temp WAV (int16); stdlib-only, adds ~a millisecond.
        import os
        import tempfile
        import wave

        import numpy as np

        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm.tobytes())
            result = self._model.transcribe(path)
        finally:
            os.unlink(path)
        return result.text.strip()


class WhisperCppTranscriber:
    name = "whisper.cpp"

    def __init__(self, model: str = "base.en"):
        from pywhispercpp.model import Model  # lazy: heavy import

        self._model = Model(model, print_progress=False, print_realtime=False)

    def transcribe(self, audio) -> str:
        segments = self._model.transcribe(audio)
        return " ".join(s.text.strip() for s in segments).strip()


def load_transcriber(prefer: Optional[str] = None) -> Transcriber:
    order = ["parakeet", "whisper"] if prefer != "whisper" else ["whisper", "parakeet"]
    errors = []
    for backend in order:
        try:
            if backend == "parakeet":
                return ParakeetTranscriber()
            return WhisperCppTranscriber()
        except Exception as e:  # noqa: BLE001 - fall through to next backend
            errors.append(f"{backend}: {e}")
    raise RuntimeError(
        "no transcriber backend available; install one of:\n"
        "  uv add parakeet-mlx   (Apple Silicon, fastest)\n"
        "  uv add pywhispercpp\n" + "\n".join(errors)
    )


def record_push_to_talk() -> "tuple":
    """Blocking: Enter to start, Enter to stop. Returns (audio, spoke_seconds)."""
    import numpy as np
    import sounddevice as sd

    input("  [voice] press Enter to talk...")
    chunks: list = []
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=lambda indata, *_: chunks.append(indata.copy()),
    )
    stream.start()
    t0 = time.perf_counter()
    input("  [voice] recording -- press Enter to stop")
    stream.stop()
    stream.close()
    seconds = time.perf_counter() - t0
    if not chunks:
        return np.zeros(1, dtype="float32"), seconds
    return np.concatenate(chunks)[:, 0], seconds


async def record_and_transcribe(transcriber: Transcriber) -> tuple[str, dict]:
    """One utterance: record (blocking input in a thread) -> transcribe. Returns
    (text, timing) where timing has record_s / transcribe_ms."""
    audio, spoke_s = await asyncio.to_thread(record_push_to_talk)
    t0 = time.perf_counter()
    text = await asyncio.to_thread(transcriber.transcribe, audio)
    transcribe_ms = (time.perf_counter() - t0) * 1000
    return text, {"record_s": spoke_s, "transcribe_ms": transcribe_ms}
