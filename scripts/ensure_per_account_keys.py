#!/usr/bin/env python3
"""Phase 0 ops helper: ensure every active account has its own VK + daily quota.

Runs against a live gateway (default http://127.0.0.1:8000). Creates a VK named
after the username when missing, and sets daily_tokens when null.

  GATEWAY_URL=http://127.0.0.1:8000 \
  ADMIN_USER=admin ADMIN_PASSWORD=... \
  DAILY_TOKENS=1000000 \
  python scripts/ensure_per_account_keys.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, Request, build_opener


def main() -> int:
    base = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
    user = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD") or os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    daily = int(os.environ.get("DAILY_TOKENS", "1000000"))
    if not password:
        print("ADMIN_PASSWORD (or BOOTSTRAP_ADMIN_PASSWORD) required", file=sys.stderr)
        return 2

    cj = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj))

    def call(method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(base + path, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with opener.open(req) as r:
            raw = r.read().decode() or "null"
            return json.loads(raw)

    call("POST", "/api/auth/login", {"username": user, "password": password})
    accounts = call("GET", "/api/accounts")
    keys = call("GET", "/api/keys")
    by_owner = {}
    for k in keys:
        by_owner.setdefault(k["owner_account_id"], []).append(k)

    for acct in accounts:
        if acct.get("status") != "active":
            continue
        owned = by_owner.get(acct["id"], [])
        if not owned:
            created = call(
                "POST",
                "/api/keys",
                {
                    "name": acct["username"],
                    "owner_account_id": acct["id"],
                    "daily_tokens": daily,
                    "enabled": True,
                },
            )
            print(
                f"created VK for {acct['username']}: id={created['id']} "
                f"quota={created.get('daily_token_quota')} "
                f"plaintext_once={created.get('key','')[:16]}..."
            )
            continue
        for k in owned:
            if k.get("daily_token_quota") is None:
                call("PATCH", f"/api/keys/{k['id']}", {"daily_tokens": daily})
                print(f"set quota={daily} on {acct['username']}/{k['name']}")
            else:
                print(
                    f"ok {acct['username']}/{k['name']} "
                    f"quota={k.get('daily_token_quota')}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
