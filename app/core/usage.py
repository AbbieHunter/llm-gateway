"""Usage logging + cost estimation (M2, US-M2-06).

Every gateway call (success / error / client_disconnect / rate_limited) lands a
row in `usage_logs` so spend is attributable to VK / account / model / provider.

The streaming generator runs *after* the request coroutine returns, so it opens
its own DB session here (decoupled from the request-scoped `get_db`).
"""
from __future__ import annotations

from app.db.models import UsageLog
from app.db.session import async_session_factory


def provider_of(model: str | None) -> str:
    """Provider prefix = `model.split('/')[0]`; bare model names default to openai."""
    if not model:
        return "openai"
    return model.split("/", 1)[0] if "/" in model else "openai"


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Cheap placeholder estimate ($/token). Report-only; marked estimated."""
    return round((prompt_tokens + completion_tokens) * 1e-5, 6)


def compute_cost(model: str | None, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """Return (cost_usd, cost_is_estimated).

    Best-effort real cost for OpenAI-family models via litellm; anything else
    (or any failure) falls back to a rough estimate flagged estimated=True.
    """
    if model and model.split("/", 1)[0] in ("openai",) and prompt_tokens + completion_tokens:
        try:
            import litellm

            cost = litellm.completion_cost(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            if cost is not None:
                return float(cost), False
        except Exception:  # noqa: BLE001 - estimation is the safe fallback
            pass
    return estimate_cost(prompt_tokens, completion_tokens), True


async def log_usage(
    *,
    vk_id: str,
    account_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    status: str,
    route_alias: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    cost_is_estimated: bool = False,
) -> None:
    if cost_usd is None:
        cost_usd, cost_is_estimated = compute_cost(model, prompt_tokens, completion_tokens)
    row = UsageLog(
        vk_id=vk_id,
        account_id=account_id,
        route_alias=route_alias,
        model=model,
        provider=provider_of(model),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        latency_ms=latency_ms,
        status=status,
    )
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
