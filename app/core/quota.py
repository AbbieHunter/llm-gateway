"""Daily token quota (M2, US-M2-04/05).

Per-VK daily token hard limit backed by Redis:

- Key: `quota:{vk_id}:{YYYY-MM-DD}` (date in LOCAL timezone).
- Gate (pre-call): if the VK's `daily_token_quota` is not NULL and the current
  counter >= quota, reject with 429 + Retry-After (= seconds to local midnight).
- Count (post-call, success/error/disconnect): INCRBY actual tokens; key TTL is
  (re)set to seconds-until-local-midnight so it expires at the natural-day reset.

Note (R-arch-3 / §2.4): counting is based on the *historical cumulative* value
(previous request's sum); the in-flight request isn't pre-deducted, so a single
oversized request may marginally exceed the cap. "Hard limit" = the *next*
request past the cap is blocked. This is documented, not a bug.
"""
from __future__ import annotations

import datetime

from app.core.redis_client import get_redis

_RETRY_AFTER_KEY = "quota:{vk_id}:{date}"


def local_date_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def seconds_to_local_midnight() -> int:
    now = datetime.datetime.now()
    midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight - now).total_seconds())


def quota_key(vk_id: str, date: str | None = None) -> str:
    return _RETRY_AFTER_KEY.format(vk_id=vk_id, date=date or local_date_str())


async def check_quota(vk_id: str, quota: int | None) -> tuple[bool, int | None]:
    """Return (allowed, retry_after_seconds).

    `quota is None` => unlimited, always allowed. A missing/under-threshold
    counter is allowed; a counter >= quota is rejected with Retry-After.
    """
    if quota is None:
        return True, None
    client = get_redis()
    if client is None:
        # M2: Redis is required; if somehow absent, fail closed (block) rather
        # than silently bypass the limit.
        return False, seconds_to_local_midnight()
    current = await client.get(quota_key(vk_id))
    if current is not None and int(current) >= quota:
        return False, seconds_to_local_midnight()
    return True, None


async def incr_quota(vk_id: str, tokens: int, date: str | None = None) -> None:
    """Add `tokens` to the VK's daily counter and align its TTL to local midnight."""
    client = get_redis()
    if client is None:
        return
    key = quota_key(vk_id, date)
    await client.incrby(key, tokens)
    # Idempotent: (re)set expiry to local midnight every time so the key always
    # resets at the natural-day boundary regardless of when it was first written.
    await client.expire(key, seconds_to_local_midnight())
