"""LiteLLM adapter (M0: direct passthrough; M2: streaming + mock extensions).

`litellm` is imported lazily so the gateway can boot and serve /healthz,
/v1/models, and the missing-model 400 path even when litellm is not installed.
The live chat path requires litellm + a configured provider key.

Streaming (M2, US-M2-01): `chat_completion(..., stream=True)` returns an
async iterator of chunks (real LiteLLM wrapper, or a mock generator for tests).
The caller re-frames chunks into SSE.

Mock (M1 R4 / M2 R3): when `MOCK_PROVIDER=1` and model starts with `mock/echo`,
no real LiteLLM call is made. The model string may carry test controls as a query
string, e.g. `mock/echo?__stream=1&__error_after=2` or `mock/echo?__toolcall=1`.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from app.config import MOCK_PROVIDER
from app.core.errors import GatewayError, map_litellm_error

try:
    import litellm
    from litellm.exceptions import APIError as LiteLLM_APIError

    _LITELLM_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install
    litellm = None  # type: ignore[assignment]
    LiteLLM_APIError = Exception  # type: ignore[assignment,misc]
    _LITELLM_AVAILABLE = False


_MOCK_MODEL = "mock/echo"


def _parse_mock_params(model: str) -> tuple[str, dict[str, str]]:
    base, sep, qs = model.partition("?")
    params = dict(parse_qsl(qs)) if sep else {}
    return base, params


def _chunk_text(text: str, n: int = 3) -> list[str]:
    if not text:
        return [""]
    # Split on whitespace, regroup into ~n pieces so the stream has multiple chunks.
    words = text.split(" ")
    if len(words) <= n:
        return [w + " " for w in words]
    per = max(1, len(words) // n)
    pieces: list[str] = []
    for i in range(0, len(words), per):
        pieces.append(" ".join(words[i : i + per]) + " ")
    return pieces


def _mock_echo(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    last = messages[-1]["content"] if messages else ""
    content = f"[mock-echo] {last}"
    prompt_tokens = sum(len(m.get("content", "")) for m in messages)
    completion_tokens = len(content)
    return {
        "id": "mock-" + model.replace("/", "-"),
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _mock_echo_stream(messages: list[dict[str, Any]], params: dict[str, str], model: str = _MOCK_MODEL):
    """Async generator yielding OpenAI-style `chat.completion.chunk` dicts."""
    last = messages[-1]["content"] if messages else ""
    error_after = int(params.get("__error_after", "0") or 0)

    if params.get("__toolcall") == "1":
        # tool_calls delta transparency (US-M2-01).
        yield {
            "id": "mock-stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        yield {
            "id": "mock-stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        return

    pieces = _chunk_text(last)
    for idx, piece in enumerate(pieces):
        # Mid-stream failure injection (US-M2-02 test): emit `error_after` chunks,
        # then raise so the SSE generator must emit a structured error event.
        if error_after and idx >= error_after:
            raise RuntimeError(f"mock upstream failure after {error_after} chunks")
        yield {
            "id": "mock-stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {"index": 0, "delta": {"content": piece}, "finish_reason": None}
            ],
        }
    yield {
        "id": "mock-stream",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


async def chat_completion(
    model: str,
    messages: list[dict[str, Any]],
    stream: bool = False,
    **kwargs: Any,
) -> Any:
    if MOCK_PROVIDER:
        base, params = _parse_mock_params(model)
        # Any `{prefix}/echo` (with optional `?__...` controls) is served by the
        # mock adapter, so provider-prefixed echo models (openai/echo, etc.) work
        # for quota marking / probe tests without a real key.
        if base.endswith("/echo"):
            # Quota-injection control (M3, US-M3-01/03): raise an error that
            # `classify_error` maps to QUOTA_EXHAUSTED, so the fallback chain can
            # mark + switch providers with no real key. Works for both stream and
            # non-stream so the eager-peek failover path is also exercisable.
            if params.get("__quota") == "1":
                raise GatewayError(
                    429,
                    "insufficient_quota: account over quota (mock)",
                    "rate_limit_error",
                    "insufficient_quota",
                )
            if stream:
                return _mock_echo_stream(messages, params, model=base)
            return _mock_echo(base, messages, **kwargs)

    if not _LITELLM_AVAILABLE:
        raise GatewayError(
            503, "litellm is not installed", "api_error", "service_unavailable"
        )
    assert litellm is not None
    try:
        return await litellm.acompletion(
            model=model, messages=messages, stream=stream, **kwargs
        )
    except LiteLLM_APIError as exc:  # network / provider errors
        raise map_litellm_error(exc) from exc
    except GatewayError:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort mapping
        raise map_litellm_error(exc) from exc


async def list_models() -> list[str]:
    from app.config import KNOWN_MODELS

    return list(KNOWN_MODELS)
