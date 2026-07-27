"""Virtual Key auth for the gateway API (/v1) (M1, US-M1-06).

Parses `Authorization: Bearer sk-...`, SHA-256 hashes it, looks up the
`virtual_keys` row, and verifies it is `active`. On success injects the resolved
account id + vk id into the request state for downstream routing. Failures raise
401 with an OpenAI-style error body (consistent with the rest of /v1).

This is a FastAPI dependency (not an ASGI middleware) so it scopes cleanly to
/v1 endpoints without interfering with /api or /healthz.
"""
from __future__ import annotations

import hashlib

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VirtualKey
from app.db.session import get_db


class VKContext:
    def __init__(self, account_id: str, vk_id: str) -> None:
        self.account_id = account_id
        self.vk_id = vk_id


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_vk(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> VKContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized("missing virtual key")
    token = authorization[len("Bearer ") :].strip()
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    vk = (
        await db.execute(select(VirtualKey).where(VirtualKey.key_hash == key_hash))
    ).scalar_one_or_none()
    if vk is None or vk.status != "active":
        raise _unauthorized("invalid or disabled virtual key")
    request.state.account_id = vk.owner_account_id
    request.state.vk_id = vk.id
    return VKContext(account_id=vk.owner_account_id, vk_id=vk.id)
