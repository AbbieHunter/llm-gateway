"""Redis client (M2).

M2 makes Redis REQUIRED for the daily-token quota counter and usage accounting.
This module is the single source for the client:

- Production: `redis.asyncio.from_url(REDIS_URL)`.
- Local tests: `REDIS_FAKE=1` swaps in an in-process `fakeredis` so the same code
  paths run without a redis-server (mirrors the `MOCK_PROVIDER=1` pattern).

`ping_redis()` is called on startup and fails loud if Redis is unreachable or
unconfigured — matching the M2_DEV_PLAN R4 decision ("PING, not just URL check").
"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.config import REDIS_FAKE, REDIS_URL

_client: aioredis.Redis | None = None


def is_configured() -> bool:
    return bool(REDIS_FAKE or REDIS_URL)


def get_redis() -> aioredis.Redis | None:
    """Return the configured Redis client, or None if Redis is not configured.

    A None return means "Redis disabled" — callers (quota gate) must treat this as
    a hard failure in M2 (the gateway refuses to start without Redis).
    """
    global _client
    if _client is not None:
        return _client
    if REDIS_FAKE:
        # Lazy import so production never pulls fakeredis in.
        from fakeredis import aioredis as fake_aioredis

        _client = fake_aioredis.FakeRedis(decode_responses=True)
        return _client
    if REDIS_URL:
        _client = aioredis.from_url(REDIS_URL, decode_responses=True)
        return _client
    return None


async def ping_redis() -> None:
    """Startup liveness probe. Fails loud if Redis is missing or unreachable."""
    if not is_configured():
        raise RuntimeError(
            "M2 requires Redis: set REDIS_URL (production) or REDIS_FAKE=1 (local tests). "
            "Quota/usage accounting cannot run without it."
        )
    client = get_redis()
    assert client is not None
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Redis PING failed ({exc}); quota counter unavailable. "
            "Counts live only in Redis and are lost on restart — see M2_DEV_PLAN §5."
        ) from exc
