"""Semantic cache (M4, US-M4-01 / R1~R3).

A Tier-2 cache layered *after* the exact cache (Tier-1, M3). On an exact miss we
embed the prompt and look for a near-duplicate (cosine >= SIMILARITY_THRESHOLD)
within the **same `provider/model` scope**. A hit returns the cached response
with zero upstream cost — further savings on top of the exact cache.

Hard rules (review R1~R3):
- Exact cache (Tier-1) is ALWAYS checked first; semantic is only a miss-path.
- `seed` present => skip (deterministic requests must not be soft-reused).
- Scope is the precise model string => never serve a deepseek answer to an
  openai request, etc.
- Non-stream only (the caller is the non-stream path).
- Embedding failure => skip the layer, call upstream (never block the request).
- Redis absent => silent no-op (every lookup is a miss).
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from app.config import (
    MOCK_PROVIDER,
    SEMANTIC_CACHE_ENABLE,
    SEMANTIC_CACHE_TTL_SEC,
    SEMANTIC_EMBEDDING_API_BASE,
    SEMANTIC_EMBEDDING_API_KEY,
    SEMANTIC_EMBEDDING_MODEL,
    SIMILARITY_THRESHOLD,
)
from app.core.redis_client import get_redis


def _prompt_text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


def _fake_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding (tests / offline, R2).

    Shared tokens => similar vectors; disjoint prompts => low cosine. Good enough
    to exercise the similarity path without a real embedding API call.
    """
    dim = 256
    vec = [0.0] * dim
    for tok in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


async def _embed(text: str) -> list[float] | None:
    """Return an embedding vector, or None on failure (caller skips the layer)."""
    # Offline / test mode: deterministic fake embedding, no network.
    if SEMANTIC_EMBEDDING_MODEL == "fake" or MOCK_PROVIDER:
        return _fake_embed(text)
    try:
        import litellm

        kwargs: dict = {"input": text}
        model = SEMANTIC_EMBEDDING_MODEL
        if SEMANTIC_EMBEDDING_API_BASE:
            # Local OpenAI-compatible embedding server (bge-small-zh-v1.5, etc.).
            # Force the openai/ route so LiteLLM talks to the custom api_base.
            kwargs["api_base"] = SEMANTIC_EMBEDDING_API_BASE
            kwargs["api_key"] = SEMANTIC_EMBEDDING_API_KEY or "not-needed"
            if not model.startswith("openai/"):
                model = f"openai/{model}"
        resp = await litellm.aembedding(model=model, **kwargs)
        return list(resp.data[0]["embedding"])
    except Exception:  # noqa: BLE001 - degrade gracefully (R2)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def sem_cache_get(
    model: str,
    messages: list[dict],
    seed: int | None,
    enable_thinking: bool | None = None,
) -> dict | None:
    """Return a cached response if a sufficiently similar prompt was cached.

    Bypassed when disabled, or when `seed` is set (deterministic, R1).
    `enable_thinking` scopes the similarity set so thinking/non-thinking
    responses are never cross-served (they differ in output-determining input).
    """
    if not SEMANTIC_CACHE_ENABLE or seed is not None:
        return None
    client = get_redis()
    if client is None:
        return None
    text = _prompt_text(messages)
    qvec = await _embed(text)
    if qvec is None:
        return None
    scope_key = f"semcache:{model}:et{enable_thinking}"
    try:
        members = await client.smembers(scope_key)
    except Exception:  # noqa: BLE001
        return None

    best: dict | None = None
    best_sim = -1.0
    for member in members:
        entry = await _get_entry(client, member)
        if entry is None:
            continue
        sim = _cosine(qvec, entry.get("embedding", []))
        if sim >= SIMILARITY_THRESHOLD and sim > best_sim:
            best_sim = sim
            best = entry.get("response")
    return best


async def sem_cache_set(
    model: str,
    messages: list[dict],
    seed: int | None,
    response: dict,
    enable_thinking: bool | None = None,
) -> None:
    """Store `response` keyed by the prompt embedding, scoped to `model`.

    `enable_thinking` scopes the similarity set (see sem_cache_get).
    """
    if not SEMANTIC_CACHE_ENABLE or seed is not None:
        return
    client = get_redis()
    if client is None:
        return
    text = _prompt_text(messages)
    vec = await _embed(text)
    if vec is None:
        return
    h = hashlib.sha256(f"{model}|{text}|et{enable_thinking}".encode()).hexdigest()
    entry_key = f"semcache:entry:{h}"
    try:
        await client.set(
            entry_key,
            json.dumps({"embedding": vec, "response": response}),
            ex=SEMANTIC_CACHE_TTL_SEC,
        )
        await client.sadd(f"semcache:{model}:et{enable_thinking}", entry_key)
    except Exception:  # noqa: BLE001
        pass


async def _get_entry(client, member) -> dict | None:
    try:
        raw = await client.get(member)
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
