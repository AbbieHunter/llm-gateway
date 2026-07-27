#!/usr/bin/env python3
"""M1 end-to-end smoke test (R4 mock adapter, keyless).

Bootstraps the gateway (MOCK_PROVIDER=1) in-process via FastAPI TestClient,
then walks the two main acceptance chains:

  Chain A — RBAC:
    bootstrap admin -> login -> create a `user` -> user calls /api/accounts
    and MUST get 403 (US-M1-05).

  Chain B — VK auth + alias routing:
    admin creates a virtual key -> admin creates an alias `fast-chat` whose only
    candidate is `mock/echo` -> a /v1/chat/completions call authenticated with
    that VK and `model=fast-chat` MUST hit the mock candidate (US-M1-06/11/12).

Env must be set BEFORE importing the app (startup reads it). TestClient is used
as a context manager so its lifespan (init_db -> seed -> bootstrap) runs.
Exits 0 on full green, 1 on any assertion failure. CI-ready.
"""
from __future__ import annotations

import os
import sys
import tempfile

# Make the project root importable when run as `python scripts/smoke_m1.py`.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- env must be set BEFORE importing app (startup reads it) ---
os.environ["MOCK_PROVIDER"] = "1"
os.environ["REDIS_FAKE"] = "1"  # M2 makes Redis required at startup; use fake for tests
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin-secret-pw"
os.environ["JWT_SECRET"] = "test-secret"
_tmp = tempfile.mkdtemp(prefix="gw-smoke-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/gateway.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.cookies.get("gw_session")


def test_rbac_and_routing() -> None:
    with TestClient(app) as client:
        # --- Chain A: RBAC ---
        admin_cookie = _login(client, "admin", "admin-secret-pw")

        # create a normal user
        r = client.post(
            "/api/accounts",
            json={"username": "alice", "password": "alice-pw", "role": "user"},
            cookies={"gw_session": admin_cookie},
        )
        assert r.status_code == 200, f"create user failed: {r.text}"
        user_cookie = _login(client, "alice", "alice-pw")

        # user hitting admin-only endpoint => 403 (US-M1-05)
        r = client.get("/api/accounts", cookies={"gw_session": user_cookie})
        assert (
            r.status_code == 403
        ), f"expected 403 for user->/api/accounts, got {r.status_code}"
        print("[OK] user calling /api/accounts -> 403 (RBAC enforced)")

        # --- Chain B: VK auth + alias routing via mock/echo ---
        me = client.get("/api/me", cookies={"gw_session": admin_cookie}).json()
        r = client.post(
            "/api/keys",
            json={"name": "admin-key", "owner_account_id": me["id"], "enabled": True},
            cookies={"gw_session": admin_cookie},
        )
        assert r.status_code == 200, f"create key failed: {r.text}"
        plaintext = r.json()["key"]
        print("[OK] virtual key created, plaintext returned once")

        # alias routing to mock/echo
        r = client.post(
            "/api/routes",
            json={"alias": "fast-chat", "providers": ["mock/echo"], "strategy": "failover"},
            cookies={"gw_session": admin_cookie},
        )
        assert r.status_code == 200, f"create route failed: {r.text}"
        print("[OK] alias fast-chat -> [mock/echo] created")

        # call /v1 with the VK and alias
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={"model": "fast-chat", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert (
            r.status_code == 200
        ), f"chat via VK+alias failed: {r.status_code} {r.text}"
        body = r.json()
        assert "[mock-echo]" in body["choices"][0]["message"]["content"]
        print("[OK] VK-authed call with model=fast-chat hit mock/echo candidate")

        # missing VK => 401
        r = client.post(
            "/v1/chat/completions",
            json={"model": "fast-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401, f"expected 401 without VK, got {r.status_code}"
        print("[OK] /v1 without VK -> 401")

        # --- disable account cascades session revoke (R1) ---
        r = client.post("/api/auth/logout", cookies={"gw_session": admin_cookie})
        assert r.status_code == 200
        r = client.get("/api/me", cookies={"gw_session": admin_cookie})
        assert (
            r.status_code == 401
        ), f"session should be revoked after logout, got {r.status_code}"
        print("[OK] logout revoked session (reuse -> 401)")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
