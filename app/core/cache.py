"""Exact response cache (M3, US-M3-09 / R7).

Only non-stream, deterministic requests are cached. The cache key normalizes the
influence-the-output parameters so two semantically-identical requests hit the
same entry, while different `temperature` / `top_p` / `seed` produce distinct
keys (R7). `stream` is intentionally excluded from the key AND caching only
serves non-stream responses, so there is no ambiguity.

Hit returns the cached OpenAI response JSON with **zero upstream cost**. Redis
is required (M2+); if absent the cache is a silent no-op (every request is a
miss) so routing never hard-fails on a missing cache.
"""
from __future__ import annotations

import hashlib
import json

from app.config import CACHE_TTL_SEC
from app.core.redis_client import get_redis

_CACHE_KEY = "cache:{hash}"


def cache_key(
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
    enable_thinking: bool | None = None,
) -> str:
    """Stable hash of the request's output-determining inputs (R7)."""
    norm = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "seed": seed,
        "enable_thinking": enable_thinking,
    }
    blob = json.dumps(norm, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def cache_get(key: str) -> dict | None:
    client = get_redis()
    if client is None:
        return None
    try:
        raw = await client.get(_CACHE_KEY.format(hash=key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


async def cache_set(key: str, value: dict, ttl: int = CACHE_TTL_SEC) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        await client.set(_CACHE_KEY.format(hash=key), json.dumps(value), ex=ttl)
    except Exception:  # noqa: BLE001
        pass
