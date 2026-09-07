"""Persistent quarantine for quota-exhausted models under an alias.

When upstream returns QUOTA_EXHAUSTED for a candidate that belongs to a route
alias, we:

1. Remove the candidate from `model_routes.providers` (durable — survives restart
   and overnight probe recovery).
2. Insert a `quarantined_models` row so the Dashboard can show alias+model and
   offer restore / permanent delete.
3. Clear the Redis `quota_exhausted` flag so the background probe does not keep
   probing a model that is no longer in any alias.

Direct (non-alias) model calls still only set the Redis flag (no alias to edit).
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.health import clear_status
from app.core.router import refresh_route_cache
from app.db.models import ModelRoute, QuarantinedModel
from app.db.session import async_session_factory

_logger = logging.getLogger("gateway")


def _public(row: QuarantinedModel) -> dict:
    return {
        "id": row.id,
        "alias": row.alias,
        "model": row.model,
        "reason": row.reason,
        "quarantined_at": row.quarantined_at.isoformat() if row.quarantined_at else None,
    }


async def quarantine_exhausted(
    alias: str | None,
    model: str,
    *,
    reason: str = "quota_exhausted",
    db: AsyncSession | None = None,
) -> dict | None:
    """Remove `model` from `alias` providers and record a quarantine row.

    Returns the public quarantine dict if an alias row was updated, else None
    (e.g. `alias` is not a registered route — caller should keep Redis mark).
    """
    if not alias:
        return None

    if db is not None:
        return await _quarantine_in_session(db, alias, model, reason)

    async with async_session_factory() as session:
        return await _quarantine_in_session(session, alias, model, reason)


async def _quarantine_in_session(
    db: AsyncSession, alias: str, model: str, reason: str
) -> dict | None:
    route = await db.get(ModelRoute, alias)
    if route is None:
        return None

    providers = json.loads(route.providers or "[]")
    if model not in providers:
        existing = (
            await db.execute(
                select(QuarantinedModel).where(
                    QuarantinedModel.alias == alias,
                    QuarantinedModel.model == model,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await clear_status(model)
            return _public(existing)
        row = QuarantinedModel(alias=alias, model=model, reason=reason)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        await clear_status(model)
        return _public(row)

    providers = [p for p in providers if p != model]
    route.providers = json.dumps(providers)

    existing = (
        await db.execute(
            select(QuarantinedModel).where(
                QuarantinedModel.alias == alias,
                QuarantinedModel.model == model,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        row = QuarantinedModel(alias=alias, model=model, reason=reason)
        db.add(row)
    else:
        row = existing
        row.reason = reason

    await db.commit()
    await db.refresh(row)
    await refresh_route_cache(alias, db)
    await clear_status(model)
    _logger.info(
        "quarantine: removed %s from alias %s (remaining=%s)",
        model,
        alias,
        len(providers),
    )
    return _public(row)


async def list_quarantined(db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(QuarantinedModel).order_by(QuarantinedModel.quarantined_at.desc())
        )
    ).scalars().all()
    return [_public(r) for r in rows]


async def restore_quarantined(qid: str, db: AsyncSession) -> dict:
    """Put the model back onto its alias provider list (append) and drop the row."""
    row = await db.get(QuarantinedModel, qid)
    if row is None:
        raise KeyError("quarantine entry not found")

    route = await db.get(ModelRoute, row.alias)
    if route is None:
        # Alias was deleted; just drop the quarantine record.
        await db.delete(row)
        await db.commit()
        return {"ok": True, "restored": False, "detail": "alias no longer exists"}

    providers = json.loads(route.providers or "[]")
    if row.model not in providers:
        providers.append(row.model)
        route.providers = json.dumps(providers)

    alias, model = row.alias, row.model
    await db.delete(row)
    await db.commit()
    await refresh_route_cache(alias, db)
    await clear_status(model)
    return {"ok": True, "restored": True, "alias": alias, "model": model}


async def delete_quarantined(qid: str, db: AsyncSession) -> dict:
    """Permanently drop the quarantine record (model stays out of the alias)."""
    row = await db.get(QuarantinedModel, qid)
    if row is None:
        raise KeyError("quarantine entry not found")
    alias, model = row.alias, row.model
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": True, "alias": alias, "model": model}
