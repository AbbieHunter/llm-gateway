"""Per-VK in-flight concurrency gate.

Caps how many /v1/chat/completions requests a single virtual key may have
in progress at once. Excess requests get 429 immediately (no gateway queue).

- Key: `inflight:{vk_id}` (Redis integer counter).
- `VK_MAX_INFLIGHT` env: max concurrent requests per VK (default 8).
  Set to 0 (or negative) to disable the gate (unlimited).
- Crash safety: counter keys get a TTL so a leaked slot expires.
"""
from __future__ import annotations

import os

from app.core.redis_client import get_redis

_KEY = "inflight:{vk_id}"
# If a worker dies mid-request without releasing, the slot expires.
_INFLIGHT_TTL_SEC = 600


def max_inflight() -> int:
    """Current cap; re-read from env so tests can adjust without reimport."""
    return int(os.getenv("VK_MAX_INFLIGHT", "8"))


def inflight_key(vk_id: str) -> str:
    return _KEY.format(vk_id=vk_id)


async def try_acquire(vk_id: str) -> bool:
    """Reserve one in-flight slot for `vk_id`. False => over the cap."""
    limit = max_inflight()
    if limit <= 0:
        return True
    client = get_redis()
    if client is None:
        # Same posture as daily quota: Redis is required; fail closed.
        return False
    key = inflight_key(vk_id)
    n = await client.incr(key)
    await client.expire(key, _INFLIGHT_TTL_SEC)
    if n > limit:
        await client.decr(key)
        return False
    return True


async def release(vk_id: str) -> None:
    """Release one in-flight slot (no-op when gate disabled or Redis absent)."""
    limit = max_inflight()
    if limit <= 0:
        return
    client = get_redis()
    if client is None:
        return
    key = inflight_key(vk_id)
    n = await client.decr(key)
    if n is not None and int(n) < 0:
        await client.set(key, 0)
        await client.expire(key, _INFLIGHT_TTL_SEC)
