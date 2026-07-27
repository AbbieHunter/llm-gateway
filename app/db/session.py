"""Async DB session management (M1).

Uses SQLAlchemy async engine + aiosqlite driver. `get_db` is the FastAPI
dependency used by all console/VK endpoints. `init_db` is called on startup
to create tables and (via seed module) populate initial providers.
"""
from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import DATABASE_URL
from app.db.models import Base

# Ensure the sqlite file directory exists (e.g. ./data).
_db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "")
_db_dir = os.path.dirname(_db_path)
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Additive migration: M2 adds `virtual_keys.daily_token_quota`. Existing M1
        # SQLite DBs won't have it from create_all (which skips existing tables),
        # so ALTER it in if missing. Safe to run every boot.
        if not await _column_exists(conn, "virtual_keys", "daily_token_quota"):
            await conn.execute(
                text("ALTER TABLE virtual_keys ADD COLUMN daily_token_quota INTEGER")
            )
        # M5: usage_logs gains `route_alias` (the requested alias vs the actual
        # model). Additive ALTER so existing DBs pick it up without a full rebuild.
        if not await _column_exists(conn, "usage_logs", "route_alias"):
            await conn.execute(
                text("ALTER TABLE usage_logs ADD COLUMN route_alias STRING")
            )


async def _column_exists(conn, table: str, column: str) -> bool:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    rows = result.fetchall()
    return any(r[1] == column for r in rows)
