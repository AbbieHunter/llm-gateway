"""Console (/api) session auth + RBAC (M1, US-M1-01/03/05, R1).

`get_current_account`: reads the httpOnly session cookie, verifies the JWT, then
looks up the `sessions` row to enforce `revoked=False` (R1). A revoked/expired/
missing session => 401. This DB check is the source of truth — frontend menu
hiding is UX only (US-M1-05).

`require_admin`: 403 for non-admin. `filter_by_owner` is the ownership guard so a
`user` only ever sees its own virtual keys.
"""
from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SecurityError, decode_token
from app.db.models import Account, Session
from app.db.session import get_db

_COOKIE_NAME = "gw_session"


def session_cookie_name() -> str:
    return _COOKIE_NAME


async def get_current_account(
    gw_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> Account:
    if not gw_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
        )
    try:
        payload = decode_token(gw_session)
    except SecurityError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session"
        )

    session = (
        await db.execute(select(Session).where(Session.jti == payload["jti"]))
    ).scalar_one_or_none()
    if session is None or session.revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session revoked"
        )

    account = await db.get(Account, payload["sub"])
    if account is None or account.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="account unavailable"
        )
    return account


async def require_admin(
    account: Account = Depends(get_current_account),
) -> Account:
    if account.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin only"
        )
    return account


def owner_filter(query, account: Account, owner_column):
    """Restrict a SQLAlchemy query to the caller's own rows (user) or leave it
    open (admin). The backend is the only enforcer of ownership (US-M1-05)."""
    if account.role == "admin":
        return query
    return query.where(owner_column == account.id)
