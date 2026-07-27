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
import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.health import get_status
from app.core.pricing import sort_by_cost
from app.core.resilience import is_open
from app.db.models import ModelRoute, Provider


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


async def resolve(
    target_model: str, db: AsyncSession, est_prompt_tokens: int | None = None
) -> list[str]:
    route = await db.get(ModelRoute, target_model)
    if route is not None:
        candidates = json.loads(route.providers)
        strategy = route.strategy
    else:
        candidates = [target_model]
        strategy = "failover"

    available: list[str] = []
    for cand in candidates:
        prefix = _provider_prefix(cand)
        provider = await db.get(Provider, prefix)
        if provider is not None and not provider.enabled:
            continue  # skip disabled provider (DB)
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
            provider = await db.get(Provider, _provider_prefix(cand))
            weights.append(provider.weight if provider else 1.0)
        available = _weighted_order(available, weights)
    elif strategy == "cost" and len(available) > 1:
        # M4 (R5): cheapest reachable candidate first. Missing price => sorted
        # after priced ones but still selectable as a fallback; tie-break keeps
        # declared order.
        available = await sort_by_cost(available, est_prompt_tokens)

    return available
