"""Console API (/api) — accounts, auth, virtual keys, providers (M1).

Endpoints:
- POST /api/auth/login   (public)   -> Set-Cookie session
- POST /api/auth/logout  (auth)     -> revoke session + clear cookie
- GET  /api/me           (auth)     -> current account
- /api/accounts          (admin)    -> CRUD accounts, disable cascades session revoke (R1)
- /api/keys              (admin=all / user=self) -> VK lifecycle (T-05)
- /api/providers         (admin)    -> provider config, only auth_ref + enabled (R7)

RBAC note (US-M1-05): every admin endpoint depends on `require_admin`; VK reads
apply `owner_filter` so a `user` only sees its own keys. Frontend menu hiding is
UX only — the backend is the single source of truth.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import secrets

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.health import list_flagged, set_status
from app.core.resilience import reset_circuit
from app.core.security import (
    SecurityError,
    create_session,
    decode_token,
    revoke_all_for_account,
    revoke_session,
    verify_password,
)
from app.db.models import Account, ModelPrice, Provider, UsageLog, VirtualKey
from app.db.session import get_db
from app.middleware.session_auth import (
    get_current_account,
    owner_filter,
    require_admin,
    session_cookie_name,
)

router = APIRouter(prefix="/api", tags=["console"])


# ---------- request schemas ----------

class LoginIn(BaseModel):
    username: str
    password: str


class AccountCreate(BaseModel):
    username: str
    password: str
    role: str = Field(default="user", pattern="^(admin|user)$")


class AccountPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    role: str | None = Field(default=None, pattern="^(admin|user)$")


class KeyCreate(BaseModel):
    name: str | None = None
    owner_account_id: str
    daily_tokens: int | None = None
    enabled: bool = True


class KeyPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    daily_tokens: int | None = None  # M2: update daily token quota (NULL = unlimited)


class ProviderCreate(BaseModel):
    id: str
    display_name: str | None = None
    auth_ref: str  # provider prefix, e.g. openai (R7)
    weight: float = 1.0
    enabled: bool = True


class ProviderPatch(BaseModel):
    display_name: str | None = None
    auth_ref: str | None = None
    weight: float | None = None
    enabled: bool | None = None


# ---------- auth ----------

@router.post("/auth/login")
async def login(body: LoginIn, response: Response, db: AsyncSession = Depends(get_db)):
    account = (
        await db.execute(select(Account).where(Account.username == body.username))
    ).scalar_one_or_none()
    if (
        account is None
        or not verify_password(body.password, account.password_hash)
        or account.status != "active"
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    _, token = await create_session(account.id)
    response.set_cookie(
        key=session_cookie_name(),
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60,  # 60 min; mirrors SESSION_EXPIRE_MIN default
    )
    return {"ok": True}


@router.post("/auth/logout")
async def logout(
    response: Response,
    gw_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    if gw_session:
        try:
            payload = decode_token(gw_session)
            await revoke_session(payload["jti"])
        except SecurityError:
            pass
    response.delete_cookie(session_cookie_name())
    return {"ok": True}


@router.get("/me")
async def me(account: Account = Depends(get_current_account)):
    return {
        "id": account.id,
        "username": account.username,
        "role": account.role,
        "status": account.status,
    }


# ---------- accounts (admin) ----------

@router.get("/accounts")
async def list_accounts(
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(Account))).scalars().all()
    return [
        {
            "id": a.id,
            "username": a.username,
            "role": a.role,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.post("/accounts")
async def create_account(
    body: AccountCreate,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = (
        await db.execute(select(Account).where(Account.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username exists")
    from app.core.security import hash_password

    account = Account(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        status="active",
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return {
        "id": account.id,
        "username": account.username,
        "role": account.role,
        "status": account.status,
    }


@router.patch("/accounts/{account_id}")
async def patch_account(
    account_id: str,
    body: AccountPatch,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    if body.status is not None:
        account.status = body.status
        # Disabling an account must instantly invalidate its sessions (R1).
        if body.status == "disabled":
            await revoke_all_for_account(account.id)
    if body.role is not None:
        account.role = body.role
    await db.commit()
    return {"id": account.id, "status": account.status, "role": account.role}


# ---------- virtual keys (T-05) ----------

def _mask(vk: VirtualKey) -> str:
    return f"sk-****{vk.key_hash[-4:]}"


def _vk_public(vk: VirtualKey) -> dict:
    return {
        "id": vk.id,
        "name": vk.name,
        "masked_key": _mask(vk),
        "owner_account_id": vk.owner_account_id,
        "status": vk.status,
        "daily_token_quota": vk.daily_token_quota,
        "quota_policy": json.loads(vk.quota_policy) if vk.quota_policy else None,
        "created_at": vk.created_at.isoformat() if vk.created_at else None,
    }


@router.post("/keys")
async def create_key(
    body: KeyCreate,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    plaintext = "sk-" + secrets.token_hex(32)
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    quota = {"daily_tokens": body.daily_tokens}
    vk = VirtualKey(
        key_hash=key_hash,
        name=body.name,
        owner_account_id=body.owner_account_id,
        status="active" if body.enabled else "disabled",
        daily_token_quota=body.daily_tokens,
        quota_policy=json.dumps(quota),
    )
    db.add(vk)
    await db.commit()
    await db.refresh(vk)
    # Plaintext returned EXACTLY once (never stored, never logged).
    return {**_vk_public(vk), "key": plaintext}


@router.get("/keys")
async def list_keys(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
):
    query = select(VirtualKey)
    query = owner_filter(query, account, VirtualKey.owner_account_id)
    rows = (await db.execute(query)).scalars().all()
    return [_vk_public(vk) for vk in rows]


@router.get("/keys/{key_id}")
async def get_key(
    key_id: str,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
):
    vk = await db.get(VirtualKey, key_id)
    if vk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
    if account.role != "admin" and vk.owner_account_id != account.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return _vk_public(vk)


@router.patch("/keys/{key_id}")
async def patch_key(
    key_id: str,
    body: KeyPatch,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    vk = await db.get(VirtualKey, key_id)
    if vk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
    if body.status is not None:
        vk.status = body.status
    if body.daily_tokens is not None:
        vk.daily_token_quota = body.daily_tokens
    await db.commit()
    return _vk_public(vk)


@router.delete("/keys/{key_id}")
async def delete_key(
    key_id: str,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    vk = await db.get(VirtualKey, key_id)
    if vk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
    await db.delete(vk)
    await db.commit()
    return {"ok": True}


@router.post("/keys/{key_id}/reset")
async def reset_key(
    key_id: str,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    vk = await db.get(VirtualKey, key_id)
    if vk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
    plaintext = "sk-" + secrets.token_hex(32)
    vk.key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    # vk.id unchanged => historical usage attribution preserved.
    await db.commit()
    return {**_vk_public(vk), "key": plaintext}


# ---------- providers (T-06) ----------

@router.get("/providers")
async def list_providers(
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(Provider))).scalars().all()
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "auth_ref": p.auth_ref,  # prefix only; real key lives in env (R7)
            "weight": p.weight,
            "enabled": p.enabled,
        }
        for p in rows
    ]


@router.post("/providers")
async def create_provider(
    body: ProviderCreate,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Provider, body.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider exists")
    p = Provider(
        id=body.id,
        display_name=body.display_name,
        auth_ref=body.auth_ref,
        weight=body.weight,
        enabled=body.enabled,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {
        "id": p.id,
        "display_name": p.display_name,
        "auth_ref": p.auth_ref,
        "weight": p.weight,
        "enabled": p.enabled,
    }


@router.patch("/providers/{provider_id}")
async def patch_provider(
    provider_id: str,
    body: ProviderPatch,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Provider, provider_id)
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")
    for field in ("display_name", "auth_ref", "weight", "enabled"):
        val = getattr(body, field)
        if val is not None:
            setattr(p, field, val)
    await db.commit()
    return {
        "id": p.id,
        "display_name": p.display_name,
        "auth_ref": p.auth_ref,
        "weight": p.weight,
        "enabled": p.enabled,
    }


@router.post("/providers/{provider_id}/reset-status")
async def reset_provider_status(
    provider_id: str,
    _: Account = Depends(require_admin),
):
    """Clear a runtime status flag (quota_exhausted / degraded) back to healthy,
    and reset its circuit breaker (US-M3-05). Admin only; the next request
    re-admits the candidate.

    `provider_id` may be EITHER a Provider prefix (legacy) OR a full model string
    such as `openai/qwen-plus-2025-12-01` (Plan-B per-model quota marking). Model
    level statuses live in Redis keyed by the full candidate, NOT the `Provider`
    table, so no DB row is required — we simply clear the Redis flag + breaker.
    The caller must URL-encode the id (it may contain `/`); see api.ts.
    """
    await set_status(provider_id, "healthy")
    await reset_circuit(provider_id)
    return {"id": provider_id, "status": "healthy"}


# ---------- usage view (M2, US-M2-07) ----------

import logging

_logger = logging.getLogger("gateway")

# --- M4 T-03: CSV export limits (R6) ---
_MAX_CSV_DAYS = 90          # max query window for export
_MAX_CSV_ROWS = 100_000     # max aggregated rows in a single CSV
_DETAIL_LIMIT = 500         # max raw call-log rows returned in the 明细 view


def _csv_cell(v) -> str:
    """Force a cell to text when it could be interpreted as a spreadsheet formula.

    A cell whose first character is `= + - @` would be executed by Excel /
    LibreOffice / Sheets as a formula (CSV/formula injection). Prefixing with a
    single quote neutralises it while keeping the visible text intact (R6).
    """
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@"):
        s = "'" + s
    return s


def _usage_rows_to_csv(rows: list[dict], group_by: str) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    if group_by == "model":
        w.writerow(["model", "provider", "calls", "total_tokens", "cost_usd", "error_rate", "cost_is_estimated"])
        for r in rows:
            w.writerow([
                _csv_cell(r.get("model")), _csv_cell(r.get("provider")),
                r.get("calls", 0), r.get("total_tokens", 0), r.get("cost_usd", 0),
                r.get("error_rate", 0), bool(r.get("cost_is_estimated")),
            ])
    elif group_by == "time":
        w.writerow(["date", "calls", "total_tokens", "cost_usd", "error_rate", "cost_is_estimated"])
        for r in rows:
            w.writerow([
                _csv_cell(r.get("date")), r.get("calls", 0), r.get("total_tokens", 0),
                r.get("cost_usd", 0), r.get("error_rate", 0), bool(r.get("cost_is_estimated")),
            ])
    elif group_by == "account":
        w.writerow(["username", "account_id", "calls", "total_tokens", "cost_usd", "error_rate", "cost_is_estimated"])
        for r in rows:
            w.writerow([
                _csv_cell(r.get("username")), _csv_cell(r.get("account_id")),
                r.get("calls", 0), r.get("total_tokens", 0), r.get("cost_usd", 0),
                r.get("error_rate", 0), bool(r.get("cost_is_estimated")),
            ])
    else:  # key (default)
        w.writerow(["vk_id", "calls", "total_tokens", "cost_usd", "error_rate", "cost_is_estimated"])
        for r in rows:
            w.writerow([
                _csv_cell(r.get("vk_id")), r.get("calls", 0), r.get("total_tokens", 0),
                r.get("cost_usd", 0), r.get("error_rate", 0), bool(r.get("cost_is_estimated")),
            ])
    return buf.getvalue()


def _usage_detail_to_csv(rows: list[dict]) -> str:
    """Per-call detail CSV (view=detail). Same formula-injection guard as the
    aggregated helper — cells starting with = + - @ are neutralised (R6)."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "created_at", "route_alias", "model", "provider",
        "prompt_tokens", "completion_tokens", "total_tokens", "status",
        "vk_id", "account_id", "username",
    ])
    for r in rows:
        w.writerow([
            _csv_cell(r.get("created_at")),
            _csv_cell(r.get("route_alias")),
            _csv_cell(r.get("model")),
            _csv_cell(r.get("provider")),
            r.get("prompt_tokens", 0),
            r.get("completion_tokens", 0),
            r.get("total_tokens", 0),
            _csv_cell(r.get("status")),
            _csv_cell(r.get("vk_id")),
            _csv_cell(r.get("account_id")),
            _csv_cell(r.get("username")),
        ])
    return buf.getvalue()


@router.get("/usage")
async def usage(
    scope: str = "self",
    vk_id: str | None = None,
    account_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    group_by: str = "key",
    range: str = "week",
    view: str = "agg",
    format: str = "json",
    db: AsyncSession = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    """Aggregated usage for the console (M2 base + M3 three-dimension upgrade).

    - User: always scoped to their own account rows (owner filter is enforced;
      any `scope=global` / `account_id` / `vk_id` param from a non-admin is
      rejected with 403 + a security log — R2).
    - Admin: may pass `?scope=global` for everything, or `?vk_id=` to drill down.
    - `group_by` (R5): `key` (default) | `model` | `time` | `account`
      (`account` only valid for admin + global — per-user totals with username).
    - `range` (R5): `day` | `week` (default, ~7d) | `month` (~30d) sets the date
      window when `from_date` is not given.
    - `view`: `agg` (default, aggregated) | `detail` (raw per-call log — time,
      alias, actual model, tokens, status, and username when global).
    """
    is_admin = account.role == "admin"
    if not is_admin and (scope == "global" or account_id or vk_id):
        _logger.warning(
            "SECURITY: non-admin '%s' attempted privileged usage query "
            "scope=%s account_id=%s vk_id=%s",
            account.username,
            scope,
            account_id,
            vk_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden: usage scope restricted to your own account",
        )

    if format not in ("json", "csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="format must be json or csv"
        )
    if view not in ("agg", "detail"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="view must be agg or detail"
        )

    # `account` grouping only makes sense for admin + global; downgrade otherwise.
    if group_by == "account" and not (is_admin and scope == "global"):
        group_by = "key"

    # Derive the date window from `range` unless an explicit from_date is given.
    if from_date is None and range in ("day", "week", "month"):
        days = {"day": 1, "week": 7, "month": 30}[range]
        from_date = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()

    # Enforce max export window (R6): 90 days.
    if from_date:
        try:
            fd = datetime.date.fromisoformat(from_date)
            td = datetime.date.today() if to_date is None else datetime.date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid date (expected YYYY-MM-DD)"
            )
        if (td - fd).days > _MAX_CSV_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"usage window exceeds {_MAX_CSV_DAYS} days",
            )

    q = select(UsageLog)
    if is_admin and scope == "global":
        pass  # all rows
    elif is_admin and vk_id:
        q = q.where(UsageLog.vk_id == vk_id)
    else:
        q = q.where(UsageLog.account_id == account.id)  # self (owner filter)
    if from_date:
        q = q.where(UsageLog.created_at >= from_date)
    if to_date:
        q = q.where(UsageLog.created_at <= to_date)
    q = q.order_by(UsageLog.created_at.desc())
    rows = (await db.execute(q)).scalars().all()

    # ---- detail view: one row per call ----
    if view == "detail":
        usernames: dict[str, str] = {}
        if is_admin and scope == "global":
            for a in (await db.execute(select(Account.id, Account.username))).all():
                usernames[a[0]] = a[1]
        detail_rows = []
        for r in rows[:_DETAIL_LIMIT]:
            detail_rows.append(
                {
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "route_alias": r.route_alias,
                    "model": r.model,
                    "provider": r.provider,
                    "prompt_tokens": r.prompt_tokens or 0,
                    "completion_tokens": r.completion_tokens or 0,
                    "total_tokens": (r.prompt_tokens or 0) + (r.completion_tokens or 0),
                    "status": r.status,
                    "vk_id": r.vk_id,
                    "account_id": r.account_id,
                    "username": (
                        usernames.get(r.account_id)
                        if (is_admin and scope == "global")
                        else None
                    ),
                }
            )
        if format == "csv":
            return Response(
                content=_usage_detail_to_csv(detail_rows),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=usage_detail.csv"},
            )
        return {"group_by": "detail", "rows": detail_rows}

    # ---- aggregated view ----
    if group_by == "model":
        def gkey(r):
            return r.model or "unknown"
        def glabel(v):
            return {"model": v, "provider": (v.split("/")[0] if v else "unknown")}
    elif group_by == "time":
        def gkey(r):
            return r.created_at.strftime("%Y-%m-%d") if r.created_at else "unknown"
        def glabel(v):
            return {"date": v}
    elif group_by == "account":
        def gkey(r):
            return r.account_id or "unknown"
        def glabel(v):
            return {"account_id": v}
    else:  # key (default)
        def gkey(r):
            return r.vk_id or "unknown"
        def glabel(v):
            return {"vk_id": v}

    agg: dict[str, dict] = {}
    for r in rows:
        k = gkey(r)
        a = agg.setdefault(
            k,
            {
                **glabel(k),
                "calls": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "errors": 0,
                "cost_is_estimated": False,
            },
        )
        a["calls"] += 1
        a["total_tokens"] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        a["cost_usd"] += float(r.cost_usd or 0)
        if r.status != "success":
            a["errors"] += 1
        a["cost_is_estimated"] = a["cost_is_estimated"] or bool(r.cost_is_estimated)

    # Attach usernames for the per-account aggregation.
    if group_by == "account":
        acct_rows = (await db.execute(select(Account.id, Account.username))).all()
        accts = {a[0]: a[1] for a in acct_rows}
        for k, a in agg.items():
            a["username"] = accts.get(k, k if k != "unknown" else "未知账号")

    for a in agg.values():
        a["error_rate"] = round(a["errors"] / a["calls"], 4) if a["calls"] else 0.0
        a.pop("errors", None)

    if format == "csv":
        rows = list(agg.values())
        if len(rows) > _MAX_CSV_ROWS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"too many rows for CSV export (>{_MAX_CSV_ROWS})",
            )
        return Response(
            content=_usage_rows_to_csv(rows, group_by),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=usage_{group_by}.csv"},
        )
    return {"group_by": group_by, "rows": list(agg.values())}


# ---------- model prices (M4, US-M4-02 / R4) ----------

class ModelPriceUpsert(BaseModel):
    provider: str
    model: str
    in_usd_per_1k: float
    out_usd_per_1k: float
    currency: str = "USD"


@router.get("/model-prices")
async def list_model_prices(
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(ModelPrice))).scalars().all()
    return [
        {
            "id": p.id,
            "provider": p.provider,
            "model": p.model,
            "in_usd_per_1k": p.in_usd_per_1k,
            "out_usd_per_1k": p.out_usd_per_1k,
            "currency": p.currency,
        }
        for p in rows
    ]


@router.put("/model-prices")
async def upsert_model_price(
    body: ModelPriceUpsert,
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = (
        await db.execute(
            select(ModelPrice).where(
                ModelPrice.provider == body.provider, ModelPrice.model == body.model
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.in_usd_per_1k = body.in_usd_per_1k
        existing.out_usd_per_1k = body.out_usd_per_1k
        existing.currency = body.currency
    else:
        db.add(
            ModelPrice(
                provider=body.provider,
                model=body.model,
                in_usd_per_1k=body.in_usd_per_1k,
                out_usd_per_1k=body.out_usd_per_1k,
                currency=body.currency,
            )
        )
    await db.commit()
    return {"ok": True, "provider": body.provider, "model": body.model}


@router.get("/dashboard/overview")
async def dashboard_overview(
    _: Account = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Dashboard four-card overview + recent-anomaly list (M3, US-M3-11, R6).

    - today_calls / today_spend_usd / active_keys (MAK daily view) / error_rate
      (share of today's calls with status != success).
    - anomalies: providers flagged quota_exhausted / degraded / down (from Redis).
    """
    today_start = (
        datetime.datetime.now()
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    rows = (
        await db.execute(select(UsageLog).where(UsageLog.created_at >= today_start))
    ).scalars().all()

    total = len(rows)
    spend = sum(float(r.cost_usd or 0) for r in rows)
    errors = sum(1 for r in rows if r.status != "success")
    error_rate = (errors / total) if total else 0.0
    active_keys = len({r.vk_id for r in rows if r.vk_id})

    anomalies = await list_flagged()
    return {
        "today_calls": total,
        "today_spend_usd": round(spend, 6),
        "error_rate": round(error_rate, 4),
        "active_keys": active_keys,
        "anomalies": anomalies,
    }
