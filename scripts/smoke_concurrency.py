#!/usr/bin/env python3
"""Smoke: per-VK in-flight concurrency gate + SQLite WAL pragma.

- VK_MAX_INFLIGHT=1: second concurrent acquire fails; after release, acquire works.
- Chat path returns 429 concurrency_exceeded when the slot is held.
- SQLite connections apply journal_mode=WAL.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ["MOCK_PROVIDER"] = "1"
os.environ["REDIS_FAKE"] = "1"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin-secret-pw"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["VK_MAX_INFLIGHT"] = "1"
_tmp = tempfile.mkdtemp(prefix="gw-concurrency-smoke-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/gateway.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core import concurrency as vk_concurrency  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _admin(client: TestClient) -> str:
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin-secret-pw"},
    )
    assert r.status_code == 200
    return r.cookies.get("gw_session")


def test_acquire_release_cap() -> None:
    async def _run() -> None:
        # Reset any leftover counter from prior tests on shared fakeredis.
        client = get_redis()
        assert client is not None
        await client.delete(vk_concurrency.inflight_key("vk-a"))

        assert await vk_concurrency.try_acquire("vk-a") is True
        assert await vk_concurrency.try_acquire("vk-a") is False
        await vk_concurrency.release("vk-a")
        assert await vk_concurrency.try_acquire("vk-a") is True
        await vk_concurrency.release("vk-a")
        # Different VK is independent.
        assert await vk_concurrency.try_acquire("vk-b") is True
        assert await vk_concurrency.try_acquire("vk-a") is True
        await vk_concurrency.release("vk-a")
        await vk_concurrency.release("vk-b")

    asyncio.run(_run())
    print("[OK] inflight acquire/release respects VK_MAX_INFLIGHT=1")


def test_chat_429_when_slot_held(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "conc-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    vk_id = rk["id"]
    client.post(
        "/api/routes",
        json={"alias": "conc", "providers": ["mock/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )

    async def _hold() -> None:
        client_r = get_redis()
        assert client_r is not None
        await client_r.delete(vk_concurrency.inflight_key(vk_id))
        assert await vk_concurrency.try_acquire(vk_id) is True

    asyncio.run(_hold())

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "conc", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["error"]["code"] == "concurrency_exceeded"
    assert r.headers.get("Retry-After") == "1"

    async def _release() -> None:
        await vk_concurrency.release(vk_id)

    asyncio.run(_release())

    r2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "conc", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r2.status_code == 200, r2.text
    print("[OK] chat returns 429 concurrency_exceeded while slot held; succeeds after release")


def test_sqlite_wal_pragma() -> None:
    async def _run() -> None:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            assert str(mode).lower() == "wal", f"expected wal, got {mode}"
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert int(busy) >= 30000, f"expected busy_timeout>=30000, got {busy}"

    asyncio.run(_run())
    print("[OK] SQLite journal_mode=WAL and busy_timeout set")


if __name__ == "__main__":
    # Run via pytest for fixture support.
    raise SystemExit(pytest.main([__file__, "-q"]))
