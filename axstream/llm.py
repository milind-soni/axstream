"""Raw SSE streaming clients -- no SDK, just httpx against the wire protocols.

Both return an async iterator of text deltas. Anything that speaks the
OpenAI chat-completions protocol (OpenAI, Groq, Cerebras, Ollama, vLLM, ...)
works through stream_openai_compat; Anthropic through stream_anthropic.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import AsyncIterator, Optional

import httpx


async def stream_anthropic(
    system: str,
    user: str,
    model: str = "claude-haiku-4-5-20251001",
    api_key: Optional[str] = None,
    max_tokens: int = 2048,
) -> AsyncIterator[str]:
    api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "stream": True,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST", "https://api.anthropic.com/v1/messages", json=payload, headers=headers
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"anthropic {resp.status_code}: {body.decode()[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta["text"]


async def stream_openai_compat(
    system: str,
    user: str,
    model: str,
    api_key: Optional[str] = None,
    base_url: str = "https://api.openai.com/v1",
    max_tokens: int = 2048,
    extra: Optional[dict] = None,
) -> AsyncIterator[str]:
    api_key = api_key or os.environ["OPENAI_API_KEY"]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        **(extra or {}),
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(4):
            async with client.stream(
                "POST", f"{base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status_code == 429 and attempt < 3:
                    body = (await resp.aread()).decode()
                    delay = _retry_after_seconds(resp, body)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(f"llm {resp.status_code}: {body.decode()[:500]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    data = json.loads(data_str)
                    choices = data.get("choices") or []
                    if choices:
                        content = choices[0].get("delta", {}).get("content")
                        if content:
                            yield content
                return


def _retry_after_seconds(resp, body: str) -> float:
    """429 backoff: honor Retry-After, else the 'try again in Xs' hint, else 2s."""
    if resp.headers.get("retry-after"):
        try:
            return min(float(resp.headers["retry-after"]) + 0.2, 15.0)
        except ValueError:
            pass
    m = re.search(r"try again in ([\d.]+)(m?s)", body)
    if m:
        seconds = float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)
        return min(seconds + 0.2, 15.0)
    return 2.0


async def replay_stream(text: str, tokens_per_second: float = 40.0) -> AsyncIterator[str]:
    """Fake LLM for dry runs: replays canned text at a realistic decode speed."""
    import asyncio

    # ~4 chars per token is a fair approximation for this JSON-heavy format.
    chunk_size = 8
    delay = chunk_size / 4 / tokens_per_second
    for i in range(0, len(text), chunk_size):
        await asyncio.sleep(delay)
        yield text[i : i + chunk_size]
