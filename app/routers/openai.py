"""OpenAI-compatible gateway API (/v1) — M1 + M2 + M3.

M3 adds the quota-aware routing + resilience layer (US-M3-01~11):
- `classify_error` buckets upstream failures into QUOTA_EXHAUSTED / RATE_LIMITED
  / AUTH_ERROR / UPSTREAM_5XX / TIMEOUT (by code/message, never status alone).
- Non-stream: per-candidate fallback chain — quota-exhausted => mark + switch
  (no retry); retryable => exp-backoff retry <= RETRY_MAX then mark degraded +
  switch; auth => switch; all fail => 502 with per-candidate detail.
- Streaming: eager-peek the first chunk so a pre-token failure fails over
  seamlessly (R4); mid-stream errors still re-emit a structured error event
  (no silent disconnect, M2 behaviour preserved).
- Exact cache (R7): identical non-stream requests return the cached response
  with zero upstream cost.
- Circuit breaker (per-provider, Redis) + provider status (quota_exhausted /
  degraded) written to Redis so `router.resolve` skips bad candidates.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import RETRY_MAX
from app.core.adapters import chat_completion, list_models
from app.core.cache import cache_get, cache_key, cache_set
from app.core.errors import (
    ErrorCategory,
    GatewayError,
    classify_error,
    invalid_request,
    to_openai_error_body,
)
from app.core.health import DEGRADED, QUOTA_EXHAUSTED, set_status
from app.core.quota import check_quota, incr_quota
from app.core.resilience import backoff_sleep, record_outcome
from app.core.router import resolve
from app.core.semantic_cache import sem_cache_get, sem_cache_set
from app.core.usage import log_usage
from app.core.guardrails import (
    inbound_enabled,
    outbound_mask_enabled,
    redact_message_content,
    redact_response_content,
)
from app.core import metrics
from app.db.models import VirtualKey
from app.db.session import get_db
from app.middleware.vk_auth import VKContext, require_vk
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/v1", tags=["openai"])


class Message(BaseModel):
    role: str
    content: Any


class ChatRequest(BaseModel):
    model: Optional[str] = None  # required (M0 decision): missing => 400
    messages: list[Message]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    seed: Optional[int] = None  # R7: part of cache key (deterministic prompt)


# ---------- helpers ----------


def _prompt_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(len(m.get("content", "")) for m in messages)


def _extract_usage(resp: Any) -> dict[str, int]:
    if hasattr(resp, "usage") and resp.usage is not None:
        return {
            "prompt_tokens": resp.usage.prompt_tokens or 0,
            "completion_tokens": resp.usage.completion_tokens or 0,
        }
    if isinstance(resp, dict):
        u = resp.get("usage") or {}
        return {
            "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0),
        }
    return {"prompt_tokens": 0, "completion_tokens": 0}


def _extract_model(resp: Any, candidate: str) -> str:
    if hasattr(resp, "model") and resp.model:
        return resp.model
    if isinstance(resp, dict) and resp.get("model"):
        return resp["model"]
    return candidate


def _response_to_cacheable(resp: Any) -> dict:
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    return resp


async def _get_vk_quota(vk_id: str, db: AsyncSession) -> Optional[int]:
    vk = await db.get(VirtualKey, vk_id)
    return vk.daily_token_quota if vk is not None else None


async def _safe_aclose(stream: Any) -> None:
    try:
        if hasattr(stream, "aclose"):
            await stream.aclose()
    except Exception:  # noqa: BLE001
        pass


def _record_request_metrics(model_used: str, status: str, latency_sec: float, tokens: int) -> None:
    """Increment the observability counters for a completed gateway request (R7).

    Only aggregate counts/levels — never VK id, PII, or provider secrets.
    """
    provider = model_used.split("/", 1)[0] if "/" in model_used else "openai"
    metrics.inc_counter(
        "gateway_requests_total",
        {"model": model_used, "provider": provider, "status": status},
    )
    if status == "success":
        metrics.observe_latency(
            "gateway_request_latency_seconds",
            latency_sec,
            {"model": model_used, "provider": provider},
        )
        metrics.inc_counter(
            "gateway_tokens_total",
            {"model": model_used, "provider": provider},
            tokens,
        )


# ---------- non-stream (fallback chain + cache) ----------


async def _nonstream_response(
    req: ChatRequest, vk: VKContext, candidates: list[str], db: AsyncSession
) -> Any:
    messages = [m.model_dump() for m in req.messages]
    passthrough = _passthrough(req)
    start = time.monotonic()
    errors: list[dict[str, str]] = []

    for candidate in candidates:
        key = cache_key(
            candidate, messages, req.temperature, req.top_p, req.seed
        )

        # Exact cache hit (R7): zero upstream cost.
        cached = await cache_get(key)
        if cached is not None:
            if outbound_mask_enabled():
                cached = redact_response_content(cached)
            usage = _extract_usage(cached)
            model_used = _extract_model(cached, candidate)
            latency = int((time.monotonic() - start) * 1000)
            pt = _prompt_tokens(messages)
            await log_usage(
                vk_id=vk.vk_id,
                account_id=vk.account_id,
                route_alias=req.model,
                model=model_used,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                status="success",
                latency_ms=latency,
                cost_usd=0.0,
                cost_is_estimated=False,
            )
            await incr_quota(vk.vk_id, pt + usage["completion_tokens"])
            metrics.inc_counter("gateway_cache_hits_total", {"type": "exact"})
            _record_request_metrics(
                model_used, "success", latency, usage["prompt_tokens"] + usage["completion_tokens"]
            )
            return cached

        # Semantic cache (Tier2, M4): only on exact miss, non-stream, seed bypass.
        sem = await sem_cache_get(candidate, messages, req.seed)
        if sem is not None:
            if outbound_mask_enabled():
                sem = redact_response_content(sem)
            usage = _extract_usage(sem)
            model_used = _extract_model(sem, candidate)
            latency = int((time.monotonic() - start) * 1000)
            pt = _prompt_tokens(messages)
            await log_usage(
                vk_id=vk.vk_id,
                account_id=vk.account_id,
                route_alias=req.model,
                model=model_used,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                status="success",
                latency_ms=latency,
                cost_usd=0.0,
                cost_is_estimated=False,
            )
            await incr_quota(vk.vk_id, pt + usage["completion_tokens"])
            metrics.inc_counter("gateway_cache_hits_total", {"type": "semantic"})
            _record_request_metrics(
                model_used, "success", latency, usage["prompt_tokens"] + usage["completion_tokens"]
            )
            return sem

        attempt = 0
        while True:
            try:
                resp = await chat_completion(
                    candidate, messages, stream=False, **passthrough
                )
                if outbound_mask_enabled():
                    resp = redact_response_content(resp)
                usage = _extract_usage(resp)
                model_used = _extract_model(resp, candidate)
                latency = int((time.monotonic() - start) * 1000)
                pt = _prompt_tokens(messages)
                await log_usage(
                    vk_id=vk.vk_id,
                    account_id=vk.account_id,
                    model=model_used,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    status="success",
                    latency_ms=latency,
                )
                await incr_quota(vk.vk_id, pt + usage["completion_tokens"])
                await set_status(candidate, "healthy")
                await record_outcome(candidate, True)
                await cache_set(key, _response_to_cacheable(resp))
                await sem_cache_set(candidate, messages, req.seed, _response_to_cacheable(resp))
                _record_request_metrics(
                    model_used, "success", latency, usage["prompt_tokens"] + usage["completion_tokens"]
                )
                return resp
            except Exception as exc:  # noqa: BLE001
                cat = classify_error(exc, candidate)
                if cat == ErrorCategory.QUOTA_EXHAUSTED:
                    # Mark + switch, no retry (retrying quota is pointless).
                    # Plan-B: mark the *candidate* (full model string), NOT the
                    # provider prefix, so exhausting one model's budget only
                    # skips that model — siblings sharing the same openai/ prefix
                    # (e.g. many free models behind one compatible-mode endpoint)
                    # keep serving.
                    await set_status(candidate, QUOTA_EXHAUSTED)
                    await record_outcome(candidate, False)
                    metrics.inc_counter("gateway_quota_marked_total", {"provider": candidate})
                    errors.append(
                        {
                            "model": candidate,
                            "error": _err_msg(exc),
                            "category": cat.value,
                        }
                    )
                    break
                if cat == ErrorCategory.AUTH_ERROR:
                    # Bad key / 4xx — switch, do not retry or mark.
                    await record_outcome(candidate, False)
                    errors.append(
                        {
                            "model": candidate,
                            "error": _err_msg(exc),
                            "category": cat.value,
                        }
                    )
                    break
                # Retryable (rate limit / 5xx / timeout): backoff then retry.
                attempt += 1
                if attempt >= RETRY_MAX:
                    await set_status(candidate, DEGRADED)
                    await record_outcome(candidate, False)
                    errors.append(
                        {
                            "model": candidate,
                            "error": _err_msg(exc),
                            "category": cat.value,
                        }
                    )
                    break
                await backoff_sleep(attempt)

    detail = "; ".join(
        f"{e['model']} [{e['category']}]: {e['error']}" for e in errors
    )
    raise GatewayError(
        502,
        f"all providers failed for model '{req.model}': {detail}",
        "api_error",
        "all_providers_failed",
    )


# ---------- streaming (eager-peek failover) ----------


async def _open_stream_with_retry(
    candidate: str, messages: list[dict], passthrough: dict
) -> tuple[Any, Any, Optional[ErrorCategory], Optional[str]]:
    """Open a streaming candidate, retrying retryable pre-token failures.

    Returns (stream, first_chunk, category_if_failed, error_msg). On success the
    stream + first chunk are returned so the SSE generator can emit the first
    chunk then continue. QUOTA/AUTH failures are NOT retried (returns immediately
    so the caller switches candidate).
    """
    last_cat: Optional[ErrorCategory] = None
    last_err: Optional[str] = None
    for attempt in range(RETRY_MAX):
        try:
            stream = await chat_completion(
                candidate, messages, stream=True, **passthrough
            )
            first = await stream.__anext__()  # eager peek (R4)
            return stream, first, None, None
        except Exception as exc:  # noqa: BLE001
            cat = classify_error(exc, candidate)
            last_cat = cat
            last_err = _err_msg(exc)
            if cat in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.AUTH_ERROR):
                return None, None, cat, last_err
            if attempt < RETRY_MAX - 1:
                await backoff_sleep(attempt + 1)
    return None, None, last_cat, last_err


async def _stream_response(
    req: ChatRequest, vk: VKContext, candidates: list[str], request: Request
) -> StreamingResponse:
    messages = [m.model_dump() for m in req.messages]
    passthrough = _passthrough(req)
    start = time.monotonic()
    errors: list[dict[str, str]] = []

    for candidate in candidates:
        stream, first, cat, err = await _open_stream_with_retry(
            candidate, messages, passthrough
        )
        if stream is None:
            if cat == ErrorCategory.QUOTA_EXHAUSTED:
                await set_status(candidate, QUOTA_EXHAUSTED)
            elif cat != ErrorCategory.AUTH_ERROR:
                await set_status(candidate, DEGRADED)
            await record_outcome(candidate, False)
            errors.append(
                {"model": candidate, "error": err or "", "category": (cat or "unknown").value}
            )
            continue
        # Got a stream + first chunk: healthy + record success, then stream.
        await set_status(candidate, "healthy")
        await record_outcome(candidate, True)
        return StreamingResponse(
            _sse_generator(stream, first, candidate, vk, messages, request, start, alias=req.model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    detail = "; ".join(
        f"{e['model']} [{e['category']}]: {e['error']}" for e in errors
    )
    raise GatewayError(
        502,
        f"all providers failed for model '{req.model}': {detail}",
        "api_error",
        "all_providers_failed",
    )


def _sse_frame(chunk: Any) -> str:
    completion_tokens = 0
    model_used = None
    if hasattr(chunk, "model_dump"):
        data = chunk.model_dump_json()
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            completion_tokens = usage.completion_tokens or completion_tokens
        if getattr(chunk, "model", None):
            model_used = chunk.model
        choices = getattr(chunk, "choices", None)
        delta = getattr(choices[0], "delta", None) if choices else None
        content = getattr(delta, "content", None) if delta else None
        if content:
            completion_tokens += len(content)
    else:
        data = json.dumps(chunk)
        usage = chunk.get("usage")
        if usage:
            completion_tokens = usage.get("completion_tokens", completion_tokens)
        if chunk.get("model"):
            model_used = chunk["model"]
        delta = chunk.get("choices", [{}])[0].get("delta", {})
        content = delta.get("content") if isinstance(delta, dict) else None
        if content:
            completion_tokens += len(content)
    return data, completion_tokens, model_used


async def _sse_generator(
    stream: Any,
    first: Any,
    candidate: str,
    vk: VKContext,
    messages: list[dict[str, Any]],
    request: Request,
    start: float,
    alias: str | None = None,
) -> Any:
    completion_tokens = 0
    model_used = candidate
    status = "success"
    try:
        if first is not None:
            data, ct, mu = _sse_frame(first)
            completion_tokens += ct
            if mu:
                model_used = mu
            yield f"data: {data}\n\n"
        async for chunk in stream:
            if await request.is_disconnected():
                status = "client_disconnect"
                break
            data, ct, mu = _sse_frame(chunk)
            completion_tokens += ct
            if mu:
                model_used = mu
            yield f"data: {data}\n\n"
        if status == "success":
            yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        await _safe_aclose(stream)
        status = "client_disconnect"
    except Exception as exc:  # noqa: BLE001
        err = to_openai_error_body(exc)
        yield f"data: {json.dumps(err)}\n\n"
        status = "error"
    finally:
        pt = _prompt_tokens(messages)
        latency = int((time.monotonic() - start) * 1000)
        try:
            await log_usage(
                vk_id=vk.vk_id,
                account_id=vk.account_id,
                route_alias=alias,
                model=model_used,
                prompt_tokens=pt,
                completion_tokens=completion_tokens,
                status=status,
                latency_ms=latency,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            await incr_quota(vk.vk_id, pt + completion_tokens)
        except Exception:  # noqa: BLE001
            pass
        _record_request_metrics(model_used, status, latency / 1000.0, pt + completion_tokens)


# ---------- request entry ----------


def _passthrough(req: ChatRequest) -> dict[str, Any]:
    passthrough: dict[str, Any] = {}
    if req.temperature is not None:
        passthrough["temperature"] = req.temperature
    if req.max_tokens is not None:
        passthrough["max_tokens"] = req.max_tokens
    if req.top_p is not None:
        passthrough["top_p"] = req.top_p
    if req.seed is not None:
        passthrough["seed"] = req.seed
    return passthrough


def _err_msg(exc: Exception) -> str:
    if isinstance(exc, GatewayError):
        return exc.body["error"]["message"]
    return str(exc)


@router.post("/chat/completions")
async def chat_completions(
    req: ChatRequest,
    vk: VKContext = Depends(require_vk),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
):
    if not req.model:
        raise invalid_request("model is required", code="missing_model")

    # --- daily token quota gate (pre-call) ---
    quota = await _get_vk_quota(vk.vk_id, db)
    if quota is not None:
        allowed, retry_after = await check_quota(vk.vk_id, quota)
        if not allowed:
            raise GatewayError(
                429,
                f"daily token quota ({quota}) exceeded for this key",
                "rate_limit_error",
                "quota_exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    # Inbound PII redaction (M4, R8): strip PII from the prompt before it leaves
    # for the upstream provider. Default OFF; streaming still gets inbound only.
    if inbound_enabled():
        for m in req.messages:
            m.content = redact_message_content(m.content)

    est_prompt = sum(len(str(m.content or "")) for m in req.messages) if not req.stream else None
    candidates = await resolve(req.model, db, est_prompt_tokens=est_prompt)
    if not candidates:
        raise GatewayError(
            502,
            f"no available provider for model '{req.model}' "
            f"(all candidates disabled or unhealthy)",
            "api_error",
            "no_available_provider",
        )

    if req.stream:
        return await _stream_response(req, vk, candidates, request)
    return await _nonstream_response(req, vk, candidates, db)


@router.get("/models")
async def models(
    _: VKContext = Depends(require_vk),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select as _select

    from app.db.models import ModelRoute

    base = await list_models()
    alias_rows = (await db.execute(_select(ModelRoute))).scalars().all()
    data = [{"id": m, "object": "model", "created": 0, "owned_by": "llm-gateway"} for m in base]
    for r in alias_rows:
        data.append(
            {"id": r.alias, "object": "model", "created": 0, "owned_by": "llm-gateway"}
        )
    return {"object": "list", "data": data}
