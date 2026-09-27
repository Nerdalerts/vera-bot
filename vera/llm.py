"""Provider adapters over plain HTTPS (httpx), no vendor SDKs.

Why no SDKs: fewer pinned dependencies to break at deploy time, a fully
async path (the reference kit used the *sync* Anthropic client inside an
async handler, which blocks the event loop and serialises every request),
and one place to enforce timeouts.

Both adapters return the model's raw text; parse_json_object() turns it
into a dict or raises ValueError.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Optional

import httpx

from . import config

_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(config.LLM_CALL_TIMEOUT + 2, connect=5.0))
    return _client


class LLMError(RuntimeError):
    pass


async def _anthropic(system: str, user: str, timeout: float) -> str:
    if not config.ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY not set")
    body = {
        "model": config.LLM_MODEL,
        "max_tokens": config.LLM_MAX_TOKENS,
        "temperature": config.LLM_TEMPERATURE,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    url = f"{config.ANTHROPIC_BASE_URL.rstrip('/')}/v1/messages"
    r = await client().post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code == 400 and "temperature" in r.text:
        body.pop("temperature")  # some models fix sampling params server-side
        r = await client().post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise LLMError(f"anthropic {r.status_code}: {r.text[:300]}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


async def _openai(system: str, user: str, timeout: float) -> str:
    if not config.OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY not set")
    body = {
        "model": config.LLM_MODEL,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
        "seed": 7,  # best-effort determinism where supported
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    headers = {"authorization": f"Bearer {config.OPENAI_API_KEY}", "content-type": "application/json"}
    url = f"{config.OPENAI_BASE_URL.rstrip('/')}/chat/completions"
    r = await client().post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code == 400:
        # compatible servers vary: drop optional params they reject, retry once
        for k in ("response_format", "seed", "temperature"):
            body.pop(k, None)
        body["max_completion_tokens"] = body.pop("max_tokens")
        r = await client().post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise LLMError(f"openai-compatible {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"] or ""


PROVIDERS: dict[str, Callable[[str, str, float], Awaitable[str]]] = {
    "anthropic": _anthropic,
    "openai": _openai,
}

# Tests swap this for a scripted fake; production resolves from env.
_override: Optional[Callable[[str, str, float], Awaitable[str]]] = None


def set_override(fn: Optional[Callable[[str, str, float], Awaitable[str]]]) -> None:
    global _override
    _override = fn


async def complete(system: str, user: str, timeout: float) -> str:
    fn = _override or PROVIDERS.get(config.LLM_PROVIDER)
    if fn is None:
        raise LLMError(f"unknown LLM_PROVIDER {config.LLM_PROVIDER!r}")
    return await fn(system, user, timeout)


def is_configured() -> bool:
    if _override:
        return True
    if config.LLM_PROVIDER == "anthropic":
        return bool(config.ANTHROPIC_API_KEY)
    if config.LLM_PROVIDER == "openai":
        return bool(config.OPENAI_API_KEY)
    return False


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json_object(text: str) -> dict:
    """Strict-but-forgiving: strips code fences / leading prose, then decodes
    the first complete JSON object. Anything else is a ValueError."""
    if not text:
        raise ValueError("empty completion")
    t = _FENCE.sub("", text.strip())
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in completion")
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    if not isinstance(obj, dict):
        raise ValueError("completion JSON is not an object")
    return obj
