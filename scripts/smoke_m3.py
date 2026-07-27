#!/usr/bin/env python3
"""M3 end-to-end smoke test (R4 mock adapter + fakeredis, keyless).

Covers US-M3-01..12 (quota-aware routing / fallback / circuit breaker / probe /
manual reset / exact cache / usage 3-dim / dashboard / estimate):
- error classification: 3 quota codes -> QUOTA_EXHAUSTED; generic 429/5xx/timeout
  do NOT mislabel quota (US-M3-01/03 red line).
- quota-aware fallback: primary mock-quota transparently switches to backup (200,
  correct model) and marks the failed provider (US-M3-02).
- all-candidates-fail -> 502 with per-candidate category detail (US-M3-08).
- streaming pre-token failover: primary quota -> backup streams [DONE] (R4).
- circuit breaker state machine: failure rate trips open; success closes (US-M3-07).
- probe auto-recovery: flagged quota_exhausted provider recovers via probe sweep
  (US-M3-04); network error keeps the mark (not flipped to down).
- manual reset: admin clears the mark; non-admin -> 403 (US-M3-05).
- exact cache: identical non-stream request hits cache (zero upstream); different
  temperature -> distinct key (US-M3-09, R7).
- usage three dimensions: group_by=key|model|time (R5); dashboard overview cards.
- estimate: non-OpenAI cost flagged estimated (US-M3-12).

Env must be set BEFORE importing the app. Uses fakeredis so no redis-server is
needed. CI-ready: exits 0 on full green.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- env must be set BEFORE importing app (startup reads it) ---
os.environ["MOCK_PROVIDER"] = "1"
os.environ["REDIS_FAKE"] = "1"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin-secret-pw"
os.environ["JWT_SECRET"] = "test-secret"
_tmp = tempfile.mkdtemp(prefix="gw-m3-smoke-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/gateway.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.errors import ErrorCategory, GatewayError, classify_error  # noqa: E402
from app.core.health import get_status, set_status  # noqa: E402
from app.core.probe import _probe_once  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.core.resilience import is_open, record_outcome, reset_circuit  # noqa: E402
from app.routers import openai as openai_router  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_provider_state():
    """Isolate provider/circuit/cache state between tests (fakeredis is shared
    across the module). Without this, a prior test leaving a provider marked
    quota_exhausted would make `resolve` filter it out for later tests."""
    r = get_redis()
    if r is not None:
        asyncio.run(r.flushall())
    for pid in ("openai", "deepseek", "qwen"):
        asyncio.run(set_status(pid, "healthy"))
        asyncio.run(reset_circuit(pid))
    yield


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.cookies.get("gw_session")


def _admin(client: TestClient) -> str:
    return _login(client, "admin", "admin-secret-pw")


# ---------- T-01: error classification (quota + mislabel red line) ----------


def test_classify_quota_codes(client: TestClient) -> None:
    # Three MVP providers' quota signals must all -> QUOTA_EXHAUSTED.
    cases = [
        GatewayError(429, "insufficient_quota", "rate_limit_error", "insufficient_quota"),
        GatewayError(402, "insufficient_balance", "api_error"),
        GatewayError(429, "QuotaExceeded: account quota exceeded", "rate_limit_error"),
    ]
    for e in cases:
        assert classify_error(e) == ErrorCategory.QUOTA_EXHAUSTED
    print("[OK] quota codes (OpenAI/DeepSeek/Qwen) -> QUOTA_EXHAUSTED")


def test_classify_no_mislabel(client: TestClient) -> None:
    # Red line: generic 429 / 5xx / timeout must NOT be quota.
    assert classify_error(GatewayError(429, "rate limit exceeded")) == ErrorCategory.RATE_LIMITED
    assert classify_error(GatewayError(500, "upstream error")) == ErrorCategory.UPSTREAM_5XX
    assert classify_error(GatewayError(503, "service unavailable")) == ErrorCategory.UPSTREAM_5XX
    assert classify_error(GatewayError(401, "invalid api key")) == ErrorCategory.AUTH_ERROR
    assert classify_error(GatewayError(400, "bad request")) == ErrorCategory.AUTH_ERROR
    print("[OK] generic 429/5xx/401/400 NOT mislabeled as quota")


# ---------- T-02: quota-aware fallback (transparent switch) ----------


def test_quota_fallback_transparent(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m3fb-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={
            "alias": "m3fb",
            "providers": ["openai/echo?__quota=1", "deepseek/echo"],
            "strategy": "failover",
        },
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m3fb", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"fallback should succeed: {r.status_code} {r.text}"
    # Response came from the backup provider, not the quota-exhausted one.
    assert r.json()["model"] == "deepseek/echo"
    # The failed primary was marked (per-candidate, Plan-B); the backup is healthy.
    assert asyncio.run(get_status("openai/echo?__quota=1")) == "quota_exhausted"
    assert asyncio.run(get_status("deepseek/echo")) == "healthy"
    print("[OK] quota-aware fallback: transparent switch to backup + mark failed candidate")


def test_all_candidates_fail_502_detail(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m3all-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={
            "alias": "m3all",
            "providers": ["openai/echo?__quota=1", "deepseek/echo?__quota=1"],
            "strategy": "failover",
        },
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m3all", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 502, f"all-fail should 502: {r.status_code} {r.text}"
    body = r.json()
    assert body["error"]["code"] == "all_providers_failed"
    assert "quota_exhausted" in body["error"]["message"]
    print("[OK] all candidates fail -> 502 with per-candidate category detail")


# ---------- T-02b: Plan-B per-model quota isolation (siblings keep serving) ----------


def test_per_model_quota_isolation(client: TestClient) -> None:
    # Plan-B: a quota_exhausted mark on ONE candidate must NOT take down its
    # siblings that share the same provider prefix. This is the exact user case:
    # many free models (each with its own 1M budget) behind one openai/ compatible
    # endpoint — exhausting model A must keep model B serving.
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m3iso-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={
            "alias": "m3iso",
            "providers": ["openai/a/echo", "openai/b/echo"],
            "strategy": "failover",
        },
        cookies={"gw_session": admin},
    )
    # Simulate model A having burned its budget.
    asyncio.run(set_status("openai/a/echo", "quota_exhausted"))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m3iso", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"sibling should still serve: {r.status_code} {r.text}"
    assert r.json()["model"] == "openai/b/echo"
    # A stays exhausted; B is healthy — isolation confirmed.
    assert asyncio.run(get_status("openai/a/echo")) == "quota_exhausted"
    assert asyncio.run(get_status("openai/b/echo")) == "healthy"
    print("[OK] Plan-B: per-model quota isolation (sibling keeps serving)")


# ---------- T-02/R4: streaming pre-token failover ----------


def test_stream_pretoken_failover(client: TestClient) -> None:
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m3sf-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={
            "alias": "m3sf",
            "providers": ["openai/echo?__quota=1", "deepseek/echo"],
            "strategy": "failover",
        },
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m3sf", "stream": True, "messages": [{"role": "user", "content": "hello world"}]},
    )
    assert r.status_code == 200, f"stream failover should be 200: {r.status_code} {r.text}"
    body = r.text
    assert body.strip().endswith("data: [DONE]")
    assert "hello" in body and "world" in body
    print("[OK] streaming pre-token failover: backup streams [DONE]")


# ---------- T-03: circuit breaker state machine ----------


def test_circuit_breaker_state(client: TestClient) -> None:
    pid = "cbtest"
    asyncio.run(reset_circuit(pid))
    # Below min-samples never trips.
    for _ in range(4):
        asyncio.run(record_outcome(pid, False))
    assert asyncio.run(is_open(pid)) is False
    # Cross threshold -> open.
    asyncio.run(record_outcome(pid, False))
    asyncio.run(record_outcome(pid, False))
    assert asyncio.run(is_open(pid)) is True
    # A success closes it.
    asyncio.run(record_outcome(pid, True))
    assert asyncio.run(is_open(pid)) is False
    print("[OK] circuit breaker: trips on failure rate, closes on success")


# ---------- T-04: probe auto-recovery ----------


def test_probe_recovers_quota(client: TestClient) -> None:
    asyncio.run(set_status("openai", "quota_exhausted"))
    asyncio.run(_probe_once())
    assert asyncio.run(get_status("openai")) == "healthy"
    print("[OK] probe sweep recovers quota_exhausted -> healthy")


def test_probe_keeps_mark_on_failure() -> None:
    from app.core import probe as probe_module

    async def scenario():
        orig = probe_module.chat_completion

        # Force probe's tiny call to fail (network-like), not flip to down.
        async def _boom(model, messages, stream=False, **kw):
            raise GatewayError(500, "connection reset", "api_error")

        probe_module.chat_completion = _boom
        try:
            await set_status("qwen", "quota_exhausted")
            await probe_module._probe_provider("qwen")
            return await get_status("qwen")
        finally:
            probe_module.chat_completion = orig

    st = asyncio.run(scenario())
    assert st == "quota_exhausted", "failed probe must KEEP quota_exhausted (never flip to down)"
    print("[OK] probe failure keeps quota mark (no flip to down)")


# ---------- T-05: manual reset (admin) + RBAC ----------


def test_manual_reset_status(client: TestClient) -> None:
    admin = _admin(client)
    asyncio.run(set_status("openai", "quota_exhausted"))
    r = client.post("/api/providers/openai/reset-status", cookies={"gw_session": admin})
    assert r.status_code == 200 and r.json()["status"] == "healthy"
    assert asyncio.run(get_status("openai")) == "healthy"

    # RBAC: a normal user cannot reset.
    client.post(
        "/api/accounts",
        json={"username": "carol", "password": "carol-pw", "role": "user"},
        cookies={"gw_session": admin},
    )
    carol = _login(client, "carol", "carol-pw")
    rb = client.post("/api/providers/openai/reset-status", cookies={"gw_session": carol})
    assert rb.status_code == 403
    print("[OK] manual reset: admin clears mark; non-admin -> 403")


# ---------- T-06: exact cache ----------


def test_exact_cache_zero_upstream(client: TestClient, monkeypatch) -> None:
    # Isolate Tier-1 exact cache: disable the M4 semantic cache (Tier-2) so this
    # regression test asserts exact-cache keying only (temperature changes the
    # exact key but NOT the semantic one — Tier-2 is covered by smoke_m4).
    monkeypatch.setattr("app.core.semantic_cache.SEMANTIC_CACHE_ENABLE", False)
    admin = _admin(client)
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    rk = client.post(
        "/api/keys",
        json={"name": "m3cache-key", "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()
    vk = rk["key"]
    client.post(
        "/api/routes",
        json={"alias": "m3cache", "providers": ["mock/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    calls = {"n": 0}
    orig = openai_router.chat_completion

    async def counted(model, messages, stream=False, **kw):
        calls["n"] += 1
        return await orig(model, messages, stream=stream, **kw)

    monkeypatch.setattr(openai_router, "chat_completion", counted)

    payload = {"model": "m3cache", "stream": False, "messages": [{"role": "user", "content": "cache me"}]}
    r1 = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {vk}"}, json=payload)
    assert r1.status_code == 200
    r2 = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {vk}"}, json=payload)
    assert r2.status_code == 200
    assert calls["n"] == 1, f"second identical call should hit cache (upstream calls={calls['n']})"

    # Different temperature -> distinct cache key -> hits upstream again.
    r3 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={**payload, "temperature": 0.9},
    )
    assert r3.status_code == 200
    assert calls["n"] == 2, f"different temperature should miss cache (calls={calls['n']})"
    print("[OK] exact cache: identical request -> 1 upstream call; temp change -> 2")


# ---------- T-07: usage three dimensions + T-08 dashboard ----------


def test_usage_three_dimensions(client: TestClient) -> None:
    admin = _admin(client)
    for gb in ("key", "model", "time"):
        r = client.get(f"/api/usage?group_by={gb}", cookies={"gw_session": admin})
        assert r.status_code == 200, f"usage group_by={gb} failed: {r.text}"
        assert r.json()["group_by"] == gb
        rows = r.json()["rows"]
        assert isinstance(rows, list)
    # default view is key
    rd = client.get("/api/usage", cookies={"gw_session": admin}).json()
    assert rd["group_by"] == "key"
    print("[OK] usage three dimensions: group_by=key|model|time (default key)")


def test_dashboard_overview(client: TestClient) -> None:
    admin = _admin(client)
    r = client.get("/api/dashboard/overview", cookies={"gw_session": admin})
    assert r.status_code == 200
    d = r.json()
    for k in ("today_calls", "today_spend_usd", "error_rate", "active_keys", "anomalies"):
        assert k in d, f"dashboard missing {k}"
    assert isinstance(d["anomalies"], list)
    print("[OK] dashboard overview: four cards + anomalies present")


# ---------- T-09: estimate flag on non-OpenAI ----------


def test_estimate_flag(client: TestClient) -> None:
    admin = _admin(client)
    r = client.get("/api/usage?group_by=model", cookies={"gw_session": admin})
    rows = r.json()["rows"]
    # mock/echo costs must be flagged estimated (non-OpenAI family).
    est_rows = [x for x in rows if x.get("cost_is_estimated")]
    assert len(est_rows) >= 1, "expected at least one estimated-cost row (non-OpenAI)"
    print("[OK] estimate flag: non-OpenAI cost marked estimated")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
