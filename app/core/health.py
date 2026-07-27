"""Candidate runtime status (M1 + M3, Plan-B per-model keying).

Status lives in Redis (key `provider:{id}:status`), NOT in the relational DB
(ARCHITECTURE §4.5, R8). This module is the single source of truth for it;
`router.resolve` reads `get_status` to skip unhealthy candidates, and M3 writes
`quota_exhausted` / `degraded` / `down` here.

`id` is the FULL candidate model string (e.g. `openai/qwen-plus-2025-12-01`),
NOT just the provider prefix. Plan-B (2026-07-27) moved the key from the
provider prefix to the full candidate so multiple models sharing one `openai/`
prefix (e.g. several free models each with its own 1M budget) are tracked
independently: exhausting one model only skips that model, siblings keep serving.

Status vocabulary:
- `healthy`        — normal; eligible for routing.
- `degraded`       — failing but not quota (e.g. retries exhausted / circuit open);
                      operator-visible, routing skips it.
- `down`           — network/unreachable (rarely set; probe never flips to this).
- `quota_exhausted`— candidate model's token/balance budget spent; M3 marks it
                      and auto-recovers via probe or on next successful call.

Key naming note: M1 shipped `provider:{id}:status` and `router.resolve` already
reads it; M3 keeps this exact key (rather than the plan's aspirational
`provider_status:{id}`) so the existing routing contract is preserved. R8's
intent — status in Redis, not the DB — is satisfied either way.
"""
from __future__ import annotations

import re

import redis.asyncio as aioredis

from app.core.redis_client import get_redis
from app.core import metrics

_HEALTHY = "healthy"
DEGRADED = "degraded"
DOWN = "down"
QUOTA_EXHAUSTED = "quota_exhausted"

_STATUS_KEY = "provider:{id}:status"
_FLAGGED_RE = re.compile(r"^provider:(.+):status$")


def _get_client() -> aioredis.Redis | None:
    # Delegate to the unified client (real Redis or fakeredis for local tests).
    # Returns None when Redis is not configured => routing treats all as healthy.
    return get_redis()


async def get_status(provider_id: str) -> str:
    client = _get_client()
    if client is None:
        return _HEALTHY
    try:
        val = await client.get(_STATUS_KEY.format(id=provider_id))
    except Exception:  # noqa: BLE001
        # If Redis is misbehaving, don't block routing — treat as healthy.
        return _HEALTHY
    return val or _HEALTHY


async def set_status(provider_id: str, status: str) -> None:
    """Write the runtime status for a candidate (full model string, Plan-B).
    No-op if Redis absent."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_STATUS_KEY.format(id=provider_id), status)
    except Exception:  # noqa: BLE001
        pass
    # Mirror into the metrics gauge set (M4, R7) — pure in-process, no Redis.
    metrics.record_provider_status(provider_id, status)


async def list_flagged() -> list[dict[str, str]]:
    """Return providers whose status != healthy, as [{'id', 'status'}].

    Used by the Dashboard "近期异常" list and the probe sweep. Returns [] when
    Redis is absent (nothing to report).
    """
    client = _get_client()
    if client is None:
        return []
    out: list[dict[str, str]] = []
    try:
        async for key in client.scan_iter(match="provider:*:status"):
            key_s = key.decode() if isinstance(key, bytes) else key
            m = _FLAGGED_RE.match(key_s)
            if not m:
                continue
            val = await client.get(key_s)
            status = val.decode() if isinstance(val, bytes) else val
            if status and status != _HEALTHY:
                out.append({"id": m.group(1), "status": status})
    except Exception:  # noqa: BLE001
        return out
    return out
