"""Route resolution (M1, §2.6).

`resolve(target_model)` maps a request `model` to an ordered list of LiteLLM
model-string candidates:

- If `target_model` is a registered alias (model_routes), use its ordered
  `providers` list.
- Otherwise treat `target_model` as a concrete model -> single candidate.

Candidates whose provider is `enabled=False` (DB) or whose Redis health status
is not `healthy` are skipped (US-M1-13). For `weighted` strategy the available
candidates are returned in a weighted-random order; `failover` keeps declared
order.

M1's fallback is the naive "try next candidate" version — error-code
classification / backoff / circuit-breaker are M3 (ARCHITECTURE §4.4).
"""
from __future__ import annotations

import json
import os
import random
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.health import get_status
from app.core.pricing import sort_by_cost
from app.core.resilience import is_open
from app.db.models import ModelRoute, Provider

# --- In-memory cache for the two static small tables hit on every resolve(). ---
# ModelRoute (alias -> providers/strategy) and Provider (prefix -> enabled/weight)
# rarely change, but resolve() runs on the hot path of every request, so we cache
# them to avoid a DB round-trip per request. Health/circuit status (get_status /
# is_open) stay per-request via Redis — those are dynamic and must not be cached.
#
# Write paths (route POST/PATCH/DELETE) refresh/drop the affected entry so a change
# takes effect immediately (write-through). A short TTL is a safety net: if any
# write path misses the invalidation, entries self-heal instead of going stale
# forever. TTLs are overridable via env.
_ROUTE_TTL = float(os.getenv("ROUTE_CACHE_TTL_SEC", "60"))
_PROVIDER_TTL = float(os.getenv("PROVIDER_CACHE_TTL_SEC", "300"))

# alias -> (expire_at_monotonic, providers_or_None, strategy_or_None)
_ROUTE_CACHE: dict[str, tuple[float, list[str] | None, str | None]] = {}
# prefix -> (expire_at_monotonic, enabled, weight)
_PROVIDER_CACHE: dict[str, tuple[float, bool, float]] = {}


def _provider_prefix(model: str) -> str:
    return model.split("/", 1)[0]


def _weighted_order(items: list[str], weights: list[float]) -> list[str]:
    order: list[str] = []
    pool = list(items)
    w = list(weights)
    while pool:
        total = sum(w)
        r = random.random() * total
        cum = 0.0
        idx = 0
        for i, wi in enumerate(w):
            cum += wi
            if r <= cum:
                idx = i
                break
        order.append(pool.pop(idx))
        w.pop(idx)
    return order


async def _route_entry(target_model: str, db: AsyncSession) -> tuple[list[str] | None, str | None]:
    """Return (providers, strategy) for an alias, cached. None/None => not an alias."""
    now = time.monotonic()
    e = _ROUTE_CACHE.get(target_model)
    if e is not None and e[0] > now:
        return e[1], e[2]
    route = await db.get(ModelRoute, target_model)
    providers = json.loads(route.providers) if route is not None else None
    strategy = route.strategy if route is not None else None
    _ROUTE_CACHE[target_model] = (now + _ROUTE_TTL, providers, strategy)
    return providers, strategy


async def _provider_entry(prefix: str, db: AsyncSession) -> tuple[bool, float]:
    """Return (enabled, weight) for a provider prefix, cached. Missing => enabled, 1.0."""
    now = time.monotonic()
    e = _PROVIDER_CACHE.get(prefix)
    if e is not None and e[0] > now:
        return e[1], e[2]
    provider = await db.get(Provider, prefix)
    enabled = provider.enabled if provider is not None else True
    weight = provider.weight if provider is not None else 1.0
    _PROVIDER_CACHE[prefix] = (now + _PROVIDER_TTL, enabled, weight)
    return enabled, weight


async def resolve(
    target_model: str, db: AsyncSession, est_prompt_tokens: int | None = None
) -> list[str]:
    providers, strategy = await _route_entry(target_model, db)
    if providers is not None:
        candidates = providers
    else:
        candidates = [target_model]
        strategy = "failover"

    available: list[str] = []
    for cand in candidates:
        prefix = _provider_prefix(cand)
        enabled, _w = await _provider_entry(prefix, db)
        if not enabled:
            continue  # skip disabled provider (cached)
        # Plan-B: health/quota/circuit status is keyed by the FULL candidate
        # model string (not just the provider prefix), so a per-model budget
        # exhaustion (e.g. one free model hitting its 1M cap) skips only that
        # model and lets sibling models behind the same openai/ prefix keep
        # serving.
        if await get_status(cand) != "healthy":
            continue  # skip unhealthy / quota_exhausted / degraded (Redis)
        if await is_open(cand):
            continue  # skip circuit-open candidate (M3 resilience)
        available.append(cand)

    if strategy == "weighted" and len(available) > 1:
        weights = []
        for cand in available:
            _enabled, w = await _provider_entry(_provider_prefix(cand), db)
            weights.append(w)
        available = _weighted_order(available, weights)
    elif strategy == "cost" and len(available) > 1:
        # M4 (R5): cheapest reachable candidate first. Missing price => sorted
        # after priced ones but still selectable as a fallback; tie-break keeps
        # declared order.
        available = await sort_by_cost(available, est_prompt_tokens)

    return available


def drop_route_cache(alias: str) -> None:
    """Invalidate a single alias entry (e.g. after delete)."""
    _ROUTE_CACHE.pop(alias, None)


async def refresh_route_cache(alias: str, db: AsyncSession) -> None:
    """Write-through refresh of a single alias entry right after a successful edit."""
    route = await db.get(ModelRoute, alias)
    providers = json.loads(route.providers) if route is not None else None
    strategy = route.strategy if route is not None else None
    _ROUTE_CACHE[alias] = (time.monotonic() + _ROUTE_TTL, providers, strategy)


def invalidate_provider_cache(prefix: str) -> None:
    """Invalidate a single provider entry (no runtime provider edit API today; kept for completeness)."""
    _PROVIDER_CACHE.pop(prefix, None)
