"""Async DB session management (M1+).

Uses SQLAlchemy async engine. Drivers:
- SQLite (default): `sqlite+aiosqlite:///./data/gateway.db` with WAL + busy_timeout
- Postgres (scale-out): `postgresql+asyncpg://user:pass@host:5432/gateway`

`get_db` is the FastAPI dependency used by all console/VK endpoints. `init_db`
is called on startup to create tables and run additive column migrations.
"""
from __future__ import annotations

import os

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import DATABASE_URL, DATA_DIR
from app.db.models import Base

os.makedirs(DATA_DIR, exist_ok=True)

_IS_SQLITE = DATABASE_URL.startswith("sqlite")
_connect_args: dict = {}
if _IS_SQLITE:
    # aiosqlite: seconds to wait on a locked DB before raising OperationalError.
    _connect_args["timeout"] = 30.0

engine = create_async_engine(DATABASE_URL, echo=False, connect_args=_connect_args)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

if _IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")  # milliseconds
        cursor.close()


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Additive migrations for DBs created before these columns existed.
        if not await _column_exists(conn, "virtual_keys", "daily_token_quota"):
            await conn.execute(
                text("ALTER TABLE virtual_keys ADD COLUMN daily_token_quota INTEGER")
            )
        if not await _column_exists(conn, "usage_logs", "route_alias"):
            # SQLite accepts STRING; Postgres prefers TEXT — both work via SQLAlchemy text.
            coltype = "TEXT" if not _IS_SQLITE else "STRING"
            await conn.execute(
                text(f"ALTER TABLE usage_logs ADD COLUMN route_alias {coltype}")
            )


async def _column_exists(conn, table: str, column: str) -> bool:
    if _IS_SQLITE:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        rows = result.fetchall()
        return any(r[1] == column for r in rows)
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.first() is not None
