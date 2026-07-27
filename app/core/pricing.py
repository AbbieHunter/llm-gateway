"""Model price lookup + cost-aware route sorting (M4, US-M4-02 / R5).

The `cost` routing strategy picks the cheapest *reachable* candidate. Prices
come from `model_prices` (seeded + admin-overridable, R4). A candidate whose
price is unknown sorts AFTER priced candidates but remains selectable as a
fallback (R5: never silently drop a healthy candidate just because its price is
missing). Tie-break preserves the declared failover order.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import ModelPrice
from app.db.session import async_session_factory


def _split(model: str) -> tuple[str, str]:
    """Return (provider, model) from a LiteLLM model string.

    `openai/gpt-4o-mini` -> ("openai", "gpt-4o-mini"); a bare `gpt-4o-mini`
    defaults to provider "openai" (mirrors usage.provider_of).
    """
    if "/" in model:
        provider, name = model.split("/", 1)
        return provider, name
    return "openai", model


async def get_price(model: str) -> tuple[float, float] | None:
    """Return (in_usd_per_1k, out_usd_per_1k) for `model`, or None if unknown."""
    provider, name = _split(model)
    try:
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    select(ModelPrice).where(
                        ModelPrice.provider == provider, ModelPrice.model == name
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return float(row.in_usd_per_1k), float(row.out_usd_per_1k)
    except Exception:  # noqa: BLE001
        return None
    return None


def _est_cost(price: tuple[float, float] | None, est_prompt: int, est_completion: int) -> float:
    """Estimated cost in USD for a request; None/unknown price => +inf (sort last)."""
    if price is None:
        return float("inf")
    in_p, out_p = price
    return (in_p * est_prompt + out_p * est_completion) / 1000.0


async def sort_by_cost(candidates: list[str], est_prompt_tokens: int | None = None) -> list[str]:
    """Order `candidates` by estimated cost ascending, preserving order for ties.

    `est_prompt_tokens` lets us bias the estimate; if None we assume a fixed
    prompt size (1k) and completion = half of that, which is enough for ranking.
    Candidates with unknown price are appended after priced ones (still usable).
    """
    est_prompt = est_prompt_tokens or 1000
    est_completion = max(1, est_prompt // 2)

    scored: list[tuple[float, int, str]] = []
    for idx, cand in enumerate(candidates):
        price = await get_price(cand)
        # `idx` is the tie-break key: equal cost keeps declared order.
        scored.append((_est_cost(price, est_prompt, est_completion), idx, cand))

    # Sort by cost, then by original index (stable, deterministic tie-break).
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in scored]
