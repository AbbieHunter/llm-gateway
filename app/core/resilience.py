"""Resilience helpers (M3, US-M3-06/07): retryable classification, backoff, and
a per-provider circuit breaker backed by Redis.

Circuit breaker design (R2):
- Key `cb:{provider_id}` is a sorted set of recent attempt outcomes
  (score = attempt timestamp ms, member = "<ts>:<1|0>").
- On each outcome we trim to the window, recompute the failure rate, and if it
  exceeds the threshold (with enough samples) we "open" the breaker by writing
  `cb_open:{provider_id}` = cooldown-until timestamp.
- `is_open` returns True while the cooldown is active → `router.resolve` skips
  the provider (equivalent to `degraded`). Once the cooldown elapses the breaker
  is half-open: the next real request is allowed through as a probe; its outcome
  re-closes (success) or re-opens (failure) the breaker.

Redis is REQUIRED (M2+). If absent, the breaker is a no-op (everything allowed)
so routing never hard-blocks on a missing counter.
"""
from __future__ import annotations

import time

from app.config import (
    CB_COOLDOWN_SEC,
    CB_FAILURE_RATE,
    CB_MIN_SAMPLES,
    CB_WINDOW_SEC,
)
from app.core.errors import ErrorCategory
from app.core.redis_client import get_redis

_CB_KEY = "cb:{id}"
_CB_OPEN_KEY = "cb_open:{id}"

# Categories that are worth retrying within a single request (exp backoff + jitter).
_RETRYABLE = {
    ErrorCategory.RATE_LIMITED,
    ErrorCategory.UPSTREAM_5XX,
    ErrorCategory.TIMEOUT,
}


def should_retry(category: ErrorCategory) -> bool:
    return category in _RETRYABLE


async def backoff_sleep(attempt: int, base: float = 0.2, cap: float = 2.0) -> None:
    """Sleep for an exponential-with-jitter backoff for `attempt` (1-based)."""
    import asyncio

    if attempt <= 0:
        return
    exp = min(cap, base * (2 ** (attempt - 1)))
    jitter = exp * 0.2 * (0.5 - (time.monotonic() % 1))
    await asyncio.sleep(max(0.0, exp + jitter))


def _now_ms() -> int:
    return int(time.time() * 1000)


async def record_outcome(provider_id: str, success: bool) -> None:
    """Record an attempt outcome and update the breaker state (M3)."""
    client = get_redis()
    if client is None:
        return
    key = _CB_KEY.format(id=provider_id)
    now = _now_ms()
    member = f"{now}:{1 if success else 0}"
    try:
        await client.zadd(key, {member: now})
        window_start = now - CB_WINDOW_SEC * 1000
        await client.zremrangebyscore(key, 0, window_start)
        # Success closes the breaker and resets the window for a clean slate.
        if success:
            await client.delete(_CB_OPEN_KEY.format(id=provider_id))
            await client.delete(key)
            return
        # Failure: recompute failure rate over the window.
        members = await client.zrangebyscore(key, window_start, now)
        if not members:
            return
        attempts = len(members)
        fails = 0
        for m in members:
            s = m.decode() if isinstance(m, bytes) else m
            outcome = int(s.split(":")[-1])
            if outcome == 0:
                fails += 1
        rate = fails / attempts if attempts else 0.0
        if attempts >= CB_MIN_SAMPLES and rate > CB_FAILURE_RATE:
            open_until = now + CB_COOLDOWN_SEC * 1000
            await client.set(_CB_OPEN_KEY.format(id=provider_id), open_until)
    except Exception:  # noqa: BLE001
        pass


async def is_open(provider_id: str) -> bool:
    """Return True if the breaker is open (cooldown active) → skip this provider."""
    client = get_redis()
    if client is None:
        return False
    try:
        val = await client.get(_CB_OPEN_KEY.format(id=provider_id))
        if val is None:
            return False
        open_until = int(val.decode() if isinstance(val, bytes) else val)
        return _now_ms() < open_until
    except Exception:  # noqa: BLE001
        return False


async def reset_circuit(provider_id: str) -> None:
    """Clear breaker state (used by the manual reset-status endpoint)."""
    client = get_redis()
    if client is None:
        return
    try:
        await client.delete(_CB_KEY.format(id=provider_id))
        await client.delete(_CB_OPEN_KEY.format(id=provider_id))
    except Exception:  # noqa: BLE001
        pass
