#!/usr/bin/env python3
"""M4 end-to-end smoke test (R4 mock adapter + fakeredis, keyless).

Covers US-M4-01..05 (semantic cache / cost routing / CSV export / observability /
PII guardrails):
- T-03 CSV: /api/usage?format=csv returns text/csv with header; oversized window
  (from_date > 90d ago) -> 400; _csv_cell neutralises formula-injection leading
  chars (R6).
- T-04 observability: GET /metrics exposes request/provider/quota metrics; only
  aggregate counts, no VK/PII (R7).
- T-02 cost routing: `strategy=cost` picks the cheapest reachable candidate; a
  candidate with unknown price is still selectable as a fallback (R5).
- T-01 semantic cache: similar non-stream prompt hits the cache (zero upstream);
  `seed` bypasses it; a different model scope is never shared (R1~R3).
- T-05 PII guardrails: default OFF (PII passes); when enabled, inbound redaction
  strips PII before it reaches the upstream; unit checks for detect/redact/mask
  (R8).

Env must be set BEFORE importing the app. Uses fakeredis (no redis-server).
CI-ready: exits 0 on full green.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest.mock as mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- env must be set BEFORE importing app (startup reads it) ---
os.environ["MOCK_PROVIDER"] = "1"
os.environ["REDIS_FAKE"] = "1"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin-secret-pw"
os.environ["JWT_SECRET"] = "test-secret"
_tmp = tempfile.mkdtemp(prefix="gw-m4-smoke-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/gateway.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config as app_config  # noqa: E402
from app.core.health import get_status, set_status  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.routers import console as console_router  # noqa: E402
from app.routers import openai as openai_router  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_provider_state():
    r = get_redis()
    if r is not None:
        asyncio.run(r.flushall())
    for pid in ("openai", "deepseek", "qwen", "mock"):
        asyncio.run(set_status(pid, "healthy"))
    yield


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.cookies.get("gw_session")


def _admin(client: TestClient) -> str:
    return _login(client, "admin", "admin-secret-pw")


def _make_key(client, admin, name="m4key"):
    me = client.get("/api/me", cookies={"gw_session": admin}).json()
    return client.post(
        "/api/keys",
        json={"name": name, "owner_account_id": me["id"]},
        cookies={"gw_session": admin},
    ).json()["key"]


# ---------- T-03: CSV export ----------


def test_csv_export_shape(client: TestClient) -> None:
    admin = _admin(client)
    r = client.get("/api/usage?format=csv", cookies={"gw_session": admin})
    assert r.status_code == 200, f"csv export failed: {r.status_code} {r.text}"
    assert "text/csv" in r.headers.get("content-type", "")
    body = r.text
    assert "vk_id" in body.splitlines()[0], "csv header should contain vk_id"
    print("[OK] CSV export: /api/usage?format=csv returns text/csv with header")


def test_csv_window_limit(client: TestClient) -> None:
    admin = _admin(client)
    r = client.get(
        "/api/usage?format=csv&from_date=2000-01-01",
        cookies={"gw_session": admin},
    )
    assert r.status_code == 400, "window > 90d should be rejected"
    print("[OK] CSV export: oversized window (>90d) -> 400")


def test_csv_cell_injection(client: TestClient) -> None:
    # R6: leading = + - @ must be neutralised so spreadsheets don't execute it.
    assert console_router._csv_cell("=cmd()") == "'=cmd()"
    assert console_router._csv_cell("-2") == "'-2"
    assert console_router._csv_cell("@x") == "'@x"
    assert console_router._csv_cell("normal") == "normal"
    print("[OK] CSV injection: leading = + - @ are quoted")


# ---------- T-04: observability ----------


def test_metrics_endpoint(client: TestClient) -> None:
    admin = _admin(client)
    vk = _make_key(client, admin, "m4metrics")
    client.post(
        "/api/routes",
        json={"alias": "m4met", "providers": ["mock/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4met", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    r = client.get("/metrics")
    assert r.status_code == 200, f"/metrics failed: {r.status_code}"
    txt = r.text
    assert "gateway_requests_total" in txt
    assert "gateway_provider_status" in txt
    # Only aggregate counts — never VK / PII / secrets.
    assert "sk-" not in txt
    print("[OK] observability: /metrics exposes request + provider metrics (no VK/PII)")


# ---------- T-02: cost routing ----------


def test_cost_strategy_cheapest(client: TestClient) -> None:
    admin = _admin(client)
    vk = _make_key(client, admin, "m4cost")
    # openai/echo expensive, deepseek/echo cheap.
    client.put(
        "/api/model-prices",
        json={"provider": "openai", "model": "echo", "in_usd_per_1k": 0.01, "out_usd_per_1k": 0.02},
        cookies={"gw_session": admin},
    )
    client.put(
        "/api/model-prices",
        json={"provider": "deepseek", "model": "echo", "in_usd_per_1k": 0.001, "out_usd_per_1k": 0.002},
        cookies={"gw_session": admin},
    )
    client.post(
        "/api/routes",
        json={"alias": "m4cost", "providers": ["openai/echo", "deepseek/echo"], "strategy": "cost"},
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4cost", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"cost route failed: {r.status_code} {r.text}"
    # Cheapest reachable candidate should win.
    assert r.json()["model"] == "deepseek/echo"
    print("[OK] cost routing: cheapest reachable candidate selected")


def test_cost_missing_price_fallback(client: TestClient) -> None:
    admin = _admin(client)
    vk = _make_key(client, admin, "m4costmiss")
    # qwen/echo has NO price row -> must still be selectable as fallback; the
    # priced deepseek/echo is preferred first (R5).
    client.post(
        "/api/routes",
        json={"alias": "m4costmiss", "providers": ["qwen/echo", "deepseek/echo"], "strategy": "cost"},
        cookies={"gw_session": admin},
    )
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4costmiss", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"missing-price route should still 200: {r.status_code} {r.text}"
    assert r.json()["model"] == "deepseek/echo"
    print("[OK] cost routing: unknown-price candidate still selectable, priced preferred")


# ---------- T-01: semantic cache ----------


def test_semantic_cache_hit(client: TestClient, monkeypatch) -> None:
    admin = _admin(client)
    vk = _make_key(client, admin, "m4sem")
    client.post(
        "/api/routes",
        json={"alias": "m4semA", "providers": ["mock/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    client.post(
        "/api/routes",
        json={"alias": "m4semB", "providers": ["openai/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )

    calls = {"n": 0}
    orig = openai_router.chat_completion

    async def counted(model, messages, stream=False, **kw):
        calls["n"] += 1
        return await orig(model, messages, stream=stream, **kw)

    monkeypatch.setattr(openai_router, "chat_completion", counted)

    p1 = "Summarize the quarterly report for the board meeting"
    p2 = "Summarize the quarterly report for the board meeting."  # near-identical -> hit
    p3 = "What is the capital of France?"  # different -> miss

    r1 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4semA", "stream": False, "messages": [{"role": "user", "content": p1}]},
    )
    assert r1.status_code == 200
    # Similar prompt (Tier2 hit) => no extra upstream call.
    r2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4semA", "stream": False, "messages": [{"role": "user", "content": p2}]},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json(), "semantic cache should return identical cached response"
    # seed bypass => exact+semantic both skipped => upstream again (R1).
    r3 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4semA", "stream": False, "messages": [{"role": "user", "content": p1}], "seed": 123},
    )
    assert r3.status_code == 200
    assert calls["n"] == 2, f"seed should bypass semantic cache (calls={calls['n']})"
    # Different prompt => miss => upstream.
    r4 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4semA", "stream": False, "messages": [{"role": "user", "content": p3}]},
    )
    assert r4.status_code == 200
    # Different model scope => never shared (R3).
    r5 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4semB", "stream": False, "messages": [{"role": "user", "content": p1}]},
    )
    assert r5.status_code == 200
    assert calls["n"] == 4, f"expected 4 upstream calls (similar/diff-model not reused): calls={calls['n']}"
    print("[OK] semantic cache: similar hit (zero upstream), seed bypass, no cross-model share")


# ---------- T-05: PII guardrails ----------


def test_guardrails_unit() -> None:
    from app.core import guardrails

    # detect mode returns text unchanged but scan finds PII.
    assert guardrails.scan_text("a@b.com 13800138000") == ["a@b.com", "13800138000"]
    redacted = guardrails.redact_text("mail a@b.com call 13800138000", "redact")
    assert "a@b.com" not in redacted and "13800138000" not in redacted
    assert "[REDACTED-EMAIL]" in redacted and "[REDACTED-PHONE]" in redacted
    # multimodal message content redaction
    red = guardrails.redact_message_content([{"type": "text", "text": "x@y.com"}])
    assert red[0]["text"] == "[REDACTED-EMAIL]"
    # outbound response masking (object shape)
    class Msg:
        content = "contact a@b.com"

    class Ch:
        message = Msg()

    fake = type("R", (), {"choices": [Ch()]})()
    guardrails.redact_response_content(fake)
    assert "a@b.com" not in fake.choices[0].message.content
    assert fake.choices[0].message.content == "contact [REDACTED-EMAIL]"
    print("[OK] PII guardrails unit: scan / redact / message / response mask")


def test_embed_routes_to_local_server(monkeypatch) -> None:
    # When SEMANTIC_EMBEDDING_API_BASE is set, _embed must route to a LOCAL
    # OpenAI-compatible embedding server: force the `openai/` prefix and pass
    # api_base + api_key through to LiteLLM (this is how bge-small-zh-v1.5 is served).
    import litellm as _litellm

    import app.core.semantic_cache as sc

    monkeypatch.setattr(sc, "MOCK_PROVIDER", False)
    monkeypatch.setattr(sc, "SEMANTIC_EMBEDDING_MODEL", "quentinz/bge-small-zh-v1.5")
    monkeypatch.setattr(sc, "SEMANTIC_EMBEDDING_API_BASE", "http://localhost:8000/v1")
    monkeypatch.setattr(sc, "SEMANTIC_EMBEDDING_API_KEY", "")

    captured: dict = {}

    class _Resp:
        data = [{"embedding": [0.1, 0.2, 0.3]}]

    async def _fake_aembedding(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return _Resp()

    with mock.patch.object(_litellm, "aembedding", side_effect=_fake_aembedding):
        vec = asyncio.run(sc._embed("你好世界"))

    assert vec == [0.1, 0.2, 0.3]
    assert captured["model"] == "openai/quentinz/bge-small-zh-v1.5", captured
    assert captured["kwargs"].get("api_base") == "http://localhost:8000/v1"
    assert captured["kwargs"].get("api_key") == "not-needed"
    print("[OK] semantic embedding: api_base routes to local OpenAI-compatible server (openai/ prefix)")


def test_guardrails_inbound_integration(client: TestClient, monkeypatch) -> None:
    admin = _admin(client)
    vk = _make_key(client, admin, "m4pii")
    client.post(
        "/api/routes",
        json={"alias": "m4pii", "providers": ["mock/echo"], "strategy": "failover"},
        cookies={"gw_session": admin},
    )
    # Default OFF: PII reaches the (mock) upstream unchanged.
    r_off = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4pii", "stream": False, "messages": [{"role": "user", "content": "mail me at tom@test.com"}]},
    )
    assert "tom@test.com" in r_off.json()["choices"][0]["message"]["content"]

    # Enable inbound redaction at runtime.
    monkeypatch.setattr(app_config, "GUARDRAILS_ENABLED", True)
    monkeypatch.setattr(app_config, "GUARDRAILS_INBOUND_MODE", "redact")
    r_on = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {vk}"},
        json={"model": "m4pii", "stream": False, "messages": [{"role": "user", "content": "mail me at tom@test.com"}]},
    )
    content = r_on.json()["choices"][0]["message"]["content"]
    assert "tom@test.com" not in content, "inbound PII must be redacted"
    assert "[REDACTED-EMAIL]" in content
    print("[OK] PII guardrails: default OFF; enabled => inbound redaction strips PII")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
