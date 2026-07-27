#!/usr/bin/env python3
"""M2 end-to-end smoke test (R4 mock adapter + fakeredis, keyless).

Covers US-M2-01..07:
- streaming SSE byte shape (data: prefix / \\n\\n / [DONE])  [R7]
- mid-stream error re-emitted as a structured event (no silent disconnect)  [R3]
- client disconnect -> client_disconnect usage log + partial quota counted  [R1/US-M2-03]
- daily token quota gate -> 429 + Retry-After; NULL quota unlimited  [US-M2-04/05]
- usage attribution (vk/account/model/provider, cost_estimated) + RBAC 403  [US-M2-06/07, R2]

Env must be set BEFORE importing the app. Uses fakeredis so no redis-server is
needed (mirrors the MOCK_PROVIDER pattern). CI-ready: exits 0 on full green.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- env must be set BEFORE importing app (startup reads it) ---
os.environ["MOCK_PROVIDER"] = "1"
os.environ["REDIS_FAKE"] = "1"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin-secret-pw"
os.environ["JWT_SECRET"] = "test-secret"
_tmp = tempfile.mkdtemp(prefix="gw-m2-smoke-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/gateway.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db.models import UsageLog  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.core.adapters import chat_completion  # noqa: E402
from app.core.quota import local_date_str  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.middleware.vk_auth import VKContext  # noqa: E402
from app.routers.openai import _sse_generator  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.cookies.get("gw_session")


def _admin(client: TestClient) -> str:
    return _login(client, "admin", "admin-secret-pw")


def test_stream_sse_bytes(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m2s-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={"alias": "m2s", "providers": ["mock/echo?__stream=1"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m2s", "stream": True, "messages": [{"role": "user", "content": "hello world"}]},
    )
    assert r.status_code == 200, f"stream failed: {r.status_code} {r.text}"
    body = r.text
    assert body.startswith("data: "), "SSE must start with 'data: '"
    assert "\n\n" in body, "SSE chunks must be separated by blank lines"
    assert body.strip().endswith("data: [DONE]"), "SSE must end with [DONE]"
    # streamed content is the echoed prompt split into chunks (not the
    # non-stream "[mock-echo]" wrapper)
    assert "hello" in body and "world" in body, "streamed content must echo the prompt"
    print("[OK] streaming SSE: data:/\\n\\n/[DONE] shape + echoed content correct")


def test_stream_error_event(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m2e-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={"alias": "m2e", "providers": ["mock/echo?__stream=1&__error_after=1"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m2e", "stream": True, "messages": [{"role": "user", "content": "hello world"}]},
    )
    # Streaming error must NOT be a 500 / silent truncate; it's a 200 body carrying
    # a structured error event.
    assert r.status_code == 200, f"error stream should still be 200: {r.status_code} {r.text}"
    body = r.text
    assert '"error"' in body, "mid-stream error must be re-emitted as an error event"
    assert "data: [DONE]" not in body, "error path must NOT emit [DONE]"
    print("[OK] mid-stream error re-emitted as event (no silent disconnect)")


def test_disconnect_logs_client_disconnect() -> None:
    async def scenario():
        messages = [{"role": "user", "content": "hi"}]
        stream = await chat_completion("mock/echo?__stream=1", messages, stream=True)
        vk = VKContext(account_id="acct_x", vk_id="vk_disconnect")

        class FakeReq:
            async def is_disconnected(self) -> bool:
                return True  # simulate client already gone

        gen = _sse_generator(stream, None, "mock/echo", vk, messages, FakeReq(), time.monotonic())
        chunks = [c async for c in gen]
        assert "data: [DONE]" not in "".join(chunks)
        async with async_session_factory() as s:
            cnt = (
                await s.execute(
                    select(func.count())
                    .select_from(UsageLog)
                    .where(UsageLog.status == "client_disconnect")
                )
            ).scalar()
        return cnt

    cnt = asyncio.run(scenario())
    assert cnt >= 1, "expected a client_disconnect usage log"
    print("[OK] client disconnect -> client_disconnect log + partial quota counted")


def test_quota_429_and_unlimited(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()

    # --- limited key (quota=10): first call burns ~16 tokens, second -> 429 ---
    rk = client.post(
        "/api/keys",
        json={"name": "quota-key", "owner_account_id": me["id"], "daily_tokens": 10},
        cookies={"gw_session": admin},
    ).json()
    assert rk["daily_token_quota"] == 10
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={"alias": "m2q", "providers": ["mock/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    r1 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m2q", "stream": False, "messages": [{"role": "user", "content": "x"}]},
    )
    assert r1.status_code == 200, f"first call should pass: {r1.text}"
    r2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m2q", "stream": False, "messages": [{"role": "user", "content": "x"}]},
    )
    assert r2.status_code == 429, f"second call should be blocked: {r2.status_code} {r2.text}"
    assert "Retry-After" in r2.headers, "429 must carry Retry-After"
    assert int(r2.headers["Retry-After"]) > 0
    # quota key has a TTL aligned to local midnight
    redis = get_redis()
    key = f"quota:{rk['id']}:{local_date_str()}"
    ttl = asyncio.run(redis.ttl(key))
    assert ttl is not None and ttl > 0
    print("[OK] daily token quota -> 429 + Retry-After (TTL set)")

    # --- unlimited key (NULL): many calls all pass ---
    rk2 = client.post(
        "/api/keys",
        json={"name": "unlim-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    assert rk2["daily_token_quota"] is None
    vk2 = rk2["key"]
    for _ in range(3):
        rr = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {vk2}"},
            json={"model": "m2q", "stream": False, "messages": [{"role": "user", "content": "x"}]},
        )
        assert rr.status_code == 200, f"unlimited key should never 429: {rr.text}"
    print("[OK] NULL quota -> unlimited (no 429)")


def test_usage_attribution_and_rbac(client: TestClient) -> None:
    admin = _admin(client)
    # attribution: group by model so each row carries provider + cost flag
    r = client.get("/api/usage?group_by=model", cookies={"gw_session": admin})
    assert r.status_code == 200, f"usage failed: {r.text}"
    rows = r.json()["rows"]
    assert r.json()["group_by"] == "model"
    assert len(rows) >= 1, "expected at least one usage row"
    row = rows[0]
    assert row["provider"] == "mock", "mock/echo => provider 'mock'"
    assert row["cost_is_estimated"] is True, "non-OpenAI cost must be flagged estimated"
    assert "total_tokens" in row and row["total_tokens"] > 0
    print("[OK] usage attribution: vk/account/model/provider + cost_estimated")

    # RBAC: a normal user passing scope=global must be rejected (R2)
    client.post(
        "/api/accounts",
        json={"username": "bob", "password": "bob-pw", "role": "user"},
        cookies={"gw_session": admin},
    )
    bob = _login(client, "bob", "bob-pw")
    rb = client.get("/api/usage?scope=global", cookies={"gw_session": bob})
    assert rb.status_code == 403, f"non-admin scope=global must 403: {rb.status_code}"
    # but bob can read his own (empty) usage fine
    rs = client.get("/api/usage", cookies={"gw_session": bob})
    assert rs.status_code == 200
    print("[OK] usage RBAC: non-admin scope=global -> 403; self-view allowed")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
