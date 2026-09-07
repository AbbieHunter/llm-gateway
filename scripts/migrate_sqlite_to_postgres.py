#!/usr/bin/env python3
"""Copy metadata from a SQLite gateway.db into Postgres.

Usage (from repo root, with deps installed):

  export SOURCE_SQLITE=./data/gateway.db
  export DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway
  python scripts/migrate_sqlite_to_postgres.py

Idempotent for empty target tables; aborts if the target already has rows in
`accounts` (refuse to clobber a live DB). Does NOT copy Redis state.
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import asyncio

from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    Base,
    ModelPrice,
    ModelRoute,
    Provider,
    QuarantinedModel,
    Session as GwSession,
    UsageLog,
    VirtualKey,
)

TABLES = (
    Account,
    VirtualKey,
    Provider,
    ModelRoute,
    ModelPrice,
    QuarantinedModel,
    GwSession,
    UsageLog,
)


async def main() -> int:
    src_path = os.environ.get("SOURCE_SQLITE", "./data/gateway.db")
    dst_url = os.environ.get("DATABASE_URL", "")
    if not dst_url.startswith("postgresql"):
        print("DATABASE_URL must be postgresql+asyncpg://...", file=sys.stderr)
        return 2
    if not os.path.isfile(src_path):
        print(f"SOURCE_SQLITE not found: {src_path}", file=sys.stderr)
        return 2

    src_engine = create_engine(f"sqlite:///{src_path}")
    dst_engine = create_async_engine(dst_url, echo=False)
    dst_factory = async_sessionmaker(dst_engine, expire_on_commit=False)

    async with dst_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with dst_factory() as dst:
        n = (
            await dst.execute(text("SELECT COUNT(*) FROM accounts"))
        ).scalar_one()
        if int(n) > 0:
            print(
                f"Target already has {n} accounts — aborting to avoid overwrite.",
                file=sys.stderr,
            )
            return 3

        with Session(src_engine) as src:
            for model in TABLES:
                rows = src.execute(select(model)).scalars().all()
                for row in rows:
                    dst.merge(row)
                print(f"copied {model.__tablename__}: {len(rows)}")
        await dst.commit()

    await dst_engine.dispose()
    src_engine.dispose()
    print("OK: SQLite -> Postgres migration complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
