#!/usr/bin/env python3
"""Unit smoke for named OpenAI-compatible upstreams (no network).

  python scripts/smoke_upstreams.py
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _clear_upstream_env(token: str = "RELAY_B") -> None:
    for suffix in ("API_KEY", "API_BASE", "KIND"):
        os.environ.pop(f"UPSTREAM_{token}_{suffix}", None)


def main() -> int:
    from app.core.errors import GatewayError
    from app.core.upstreams import env_token, lookup, prepare_litellm_model

    assert env_token("relay_b") == "RELAY_B"
    assert env_token("relay-b") == "RELAY_B"

    _clear_upstream_env()
    assert lookup("relay_b") is None
    model, kwargs = prepare_litellm_model("openai/qwen-plus")
    assert model == "openai/qwen-plus" and kwargs == {}
    print("[OK] passthrough without UPSTREAM_*")

    os.environ["UPSTREAM_RELAY_B_API_KEY"] = "sk-test"
    # key alone still counts as named upstream, but openai kind requires base
    try:
        prepare_litellm_model("relay_b/qwen-plus")
        print("[FAIL] expected upstream_misconfigured without API_BASE")
        return 1
    except GatewayError as e:
        assert e.status_code == 503
        assert e.body["error"].get("code") == "upstream_misconfigured"
    print("[OK] missing API_BASE fails loud")

    os.environ["UPSTREAM_RELAY_B_API_BASE"] = "https://relay-b.example/v1"
    model, kwargs = prepare_litellm_model("relay_b/qwen-plus")
    assert model == "openai/qwen-plus", model
    assert kwargs == {
        "api_base": "https://relay-b.example/v1",
        "api_key": "sk-test",
    }, kwargs
    print("[OK] rewrite relay_b/… -> openai/… with kwargs")

    model, kwargs = prepare_litellm_model("relay_b/foo?__quota=1")
    assert model == "openai/foo"
    assert kwargs["api_base"] == "https://relay-b.example/v1"
    print("[OK] query string stripped for named upstream rewrite")

    os.environ["UPSTREAM_RELAY_B_KIND"] = "anthropic"
    try:
        prepare_litellm_model("relay_b/x")
        print("[FAIL] expected unsupported KIND")
        return 1
    except GatewayError as e:
        assert e.body["error"].get("code") == "upstream_misconfigured"
    print("[OK] unsupported KIND rejected")

    _clear_upstream_env()
    print("all upstream smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
