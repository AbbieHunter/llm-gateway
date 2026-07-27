"""SQLAlchemy ORM models (M1).

Field口径对齐 ARCHITECTURE.md §5 与 M1_DEV_PLAN.md v0.2 评审决议 R1~R7：
- `virtual_keys` 不含 `expires_at`（R6：VK 不过期，仅 status 控制）。
- `sessions` 为 R1 新增的 DB-backed 会话表（禁用账号/登出即时失效）。
- `model_routes.providers` 为有序 LiteLLM 模型串列表（R3）。
- 口令哈希用 stdlib pbkdf2（R2），`password_hash` 字段仅存派生的哈希串。
"""
from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, default="user")  # admin | user
    status: Mapped[str] = mapped_column(String, default="active")  # active | disabled
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class VirtualKey(Base):
    __tablename__ = "virtual_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # SHA-256
    name: Mapped[str | None] = mapped_column(String)
    owner_account_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="active")  # active | disabled
    # JSON: {"daily_tokens": <int|null>}. M1 stores policy only; deduction in M2 (R6).
    quota_policy: Mapped[str | None] = mapped_column(Text)
    # M2 (R-arch): dedicated daily token quota column (NULL = unlimited).
    # Authoritative source for the quota gate; quota_policy.daily_tokens kept for
    # backward-compat only.
    daily_token_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    vk_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="success")  # success|error|client_disconnect|rate_limited
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # openai / deepseek / qwen
    display_name: Mapped[str | None] = mapped_column(String)
    auth_ref: Mapped[str | None] = mapped_column(String)  # provider prefix, e.g. openai
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ModelRoute(Base):
    __tablename__ = "model_routes"

    alias: Mapped[str] = mapped_column(String, primary_key=True)  # fast-chat
    # JSON ordered list of LiteLLM model strings, e.g. ["openai/gpt-4o-mini","deepseek/deepseek-chat"]
    providers: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(String, default="failover")  # failover | weighted


class Session(Base):
    __tablename__ = "sessions"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class ModelPrice(Base):
    """Per-model unit prices (M4, US-M4-02 / R4).

    USD per 1k tokens. Seeded from a built-in baseline (see db/seed.py); admins
    may override via the console API. `effective_from` is kept for future
    price-history but MVP only ever reads the latest row (R4).
    """

    __tablename__ = "model_prices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    in_usd_per_1k: Mapped[float] = mapped_column(Float, default=0.0)
    out_usd_per_1k: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String, default="USD")
    effective_from: Mapped[datetime.datetime | None] = mapped_column(DateTime)

