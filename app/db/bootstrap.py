"""Initial admin bootstrap (M1, US-M1-00, R5 fail-loud).

On startup, if the `accounts` table is empty, create the first admin from
`BOOTSTRAP_ADMIN_PASSWORD`. If that env is missing, startup FAILS loudly —
we never create a weak default admin (R5). Subsequent boots with existing
accounts are no-ops. No public registration page is exposed.
"""
from __future__ import annotations

from sqlalchemy import select

from app.config import BOOTSTRAP_ADMIN_PASSWORD, BOOTSTRAP_ADMIN_USERNAME
from app.core.security import hash_password
from app.db.models import Account
from app.db.session import async_session_factory


async def bootstrap_admin() -> None:
    async with async_session_factory() as db:
        existing = (await db.execute(select(Account))).scalars().first()
        if existing is not None:
            return  # already initialized

        if not BOOTSTRAP_ADMIN_PASSWORD:
            raise RuntimeError(
                "BOOTSTRAP_ADMIN_PASSWORD is required to create the first admin, "
                "but it is not set. Refusing to start with an empty accounts table."
            )

        db.add(
            Account(
                username=BOOTSTRAP_ADMIN_USERNAME,
                password_hash=hash_password(BOOTSTRAP_ADMIN_PASSWORD),
                role="admin",
                status="active",
            )
        )
        await db.commit()
