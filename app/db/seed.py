"""Seed initial providers (M1, US-M1-10) + model prices (M4, US-M4-02 / R4).

If the `providers` table is empty at startup, insert the baseline set
(openai / deepseek / qwen) with only `auth_ref` + `enabled` + `weight`.
Real credentials live in env (OPENAI_API_KEY etc.), never in DB (ARCHITECTURE §4.2).

If the `model_prices` table is empty, insert a built-in baseline USD/1k price
list. Admins override via the console API; the baseline is intentionally
approximate and marked estimated in reports.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import ModelPrice, Provider
from app.db.session import async_session_factory

SEED_PROVIDERS = [
    {"id": "openai", "display_name": "OpenAI", "auth_ref": "openai", "weight": 1.0, "enabled": True},
    {"id": "deepseek", "display_name": "DeepSeek", "auth_ref": "deepseek", "weight": 1.0, "enabled": True},
    {"id": "qwen", "display_name": "通义千问", "auth_ref": "qwen", "weight": 1.0, "enabled": True},
]

# Built-in baseline prices (USD per 1k tokens). Approximate; admin-overridable.
SEED_MODEL_PRICES = [
    {"provider": "openai", "model": "gpt-4o", "in_usd_per_1k": 0.005, "out_usd_per_1k": 0.015},
    {"provider": "openai", "model": "gpt-4o-mini", "in_usd_per_1k": 0.00015, "out_usd_per_1k": 0.0006},
    {"provider": "openai", "model": "gpt-3.5-turbo", "in_usd_per_1k": 0.0005, "out_usd_per_1k": 0.0015},
    {"provider": "deepseek", "model": "deepseek-chat", "in_usd_per_1k": 0.00027, "out_usd_per_1k": 0.0011},
    {"provider": "deepseek", "model": "deepseek-reasoner", "in_usd_per_1k": 0.00055, "out_usd_per_1k": 0.0022},
    {"provider": "qwen", "model": "qwen-turbo", "in_usd_per_1k": 0.0004, "out_usd_per_1k": 0.0012},
    {"provider": "qwen", "model": "qwen-plus", "in_usd_per_1k": 0.0008, "out_usd_per_1k": 0.002},
]


async def seed_providers() -> None:
    async with async_session_factory() as db:
        result = await db.execute(select(Provider))
        if result.scalars().first() is not None:
            return
        for p in SEED_PROVIDERS:
            db.add(Provider(**p))
        await db.commit()


async def seed_model_prices() -> None:
    async with async_session_factory() as db:
        result = await db.execute(select(ModelPrice))
        if result.scalars().first() is not None:
            return
        for p in SEED_MODEL_PRICES:
            db.add(ModelPrice(**p))
        await db.commit()
