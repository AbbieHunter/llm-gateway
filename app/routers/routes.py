"""Route alias CRUD (M1, US-M1-11/12/13). Admin only.

`providers` is an ordered list of LiteLLM model strings (R3), e.g.
["openai/gpt-4o-mini","deepseek/deepseek-chat"]. Strategy is failover | weighted.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, ModelRoute
from app.db.session import get_db
from app.middleware.session_auth import require_admin

router = APIRouter(prefix="/api/routes", tags=["routes"])


class RouteCreate(BaseModel):
    alias: str
    providers: list[str] = Field(min_length=1)
    strategy: str = Field(default="failover", pattern="^(failover|weighted|cost)$")


class RoutePatch(BaseModel):
    alias: str | None = Field(default=None, min_length=1)  # optional rename target
    providers: list[str] | None = Field(default=None, min_length=1)
    strategy: str | None = Field(default=None, pattern="^(failover|weighted|cost)$")
    enabled: bool | None = None  # reserved; M1 keeps routes always active


@router.get("")
async def list_routes(
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(ModelRoute))).scalars().all()
    return [
        {
            "alias": r.alias,
            "providers": json.loads(r.providers),
            "strategy": r.strategy,
        }
        for r in rows
    ]


@router.post("")
async def create_route(
    body: RouteCreate,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(ModelRoute, body.alias) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="alias exists")
    r = ModelRoute(
        alias=body.alias,
        providers=json.dumps(body.providers),
        strategy=body.strategy,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return {"alias": r.alias, "providers": body.providers, "strategy": r.strategy}


@router.patch("/{alias}")
async def patch_route(
    alias: str,
    body: RoutePatch,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(ModelRoute, alias)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alias not found")
    # `alias` is the primary key; an edit may rename it. Resolve the rename
    # first and reject collisions with an existing alias.
    if body.alias is not None and body.alias != alias:
        if await db.get(ModelRoute, body.alias) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="alias exists")
        r.alias = body.alias
    if body.providers is not None:
        r.providers = json.dumps(body.providers)
    if body.strategy is not None:
        r.strategy = body.strategy
    await db.commit()
    await db.refresh(r)
    return {"alias": r.alias, "providers": json.loads(r.providers), "strategy": r.strategy}


@router.delete("/{alias}")
async def delete_route(
    alias: str,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(ModelRoute, alias)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alias not found")
    await db.delete(r)
    await db.commit()
    return {"ok": True}
