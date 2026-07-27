"""Error mapping seam (M0 + M3).

M0 mapped LiteLLM exceptions -> OpenAI-style error JSON + pass-through HTTP
status. M3 adds `classify_error` which buckets any upstream exception into the
four-category internal model (QUOTA_EXHAUSTED / RATE_LIMITED / AUTH_ERROR /
UPSTREAM_5XX / TIMEOUT) used by the fallback / circuit-breaker logic.

Classification is by **code/message, never by HTTP status alone** (R1): a
generic 429 must not be confused with a quota 429, and a 402 (DeepSeek) or a
provider-specific message must be recognized as quota. Raw LiteLLM exceptions
carry `llm_provider` + `response.json().error.code`/`message`; after
`map_litellm_error` runs in adapters.py the same signal is available on the
resulting `GatewayError` body, so `classify_error` primarily reads
`GatewayError.status_code` + `body.error.code` + `message`.
"""
from enum import Enum

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCategory(str, Enum):
    """Internal bucket for an upstream failure (M3, ARCHITECTURE §4.4)."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"
    UPSTREAM_5XX = "upstream_5xx"
    TIMEOUT = "timeout"


# Substrings that unequivocally signal a quota / billing exhaustion across the
# MVP providers (OpenAI `insufficient_quota`, DeepSeek `insufficient_balance`,
# Qwen `QuotaExceeded` / `AccountBalanceInsufficient`). Matched case-insensitively
# against the error code + message (R1). NOTE: the LiteLLM
# `RateLimitErrorCategory` enum has NO quota-specific value, so we must inspect
# the raw body/message — never the category (spike-verified on litellm 1.93).
_QUOTA_TOKENS = (
    "insufficient_quota",
    "insufficient_balance",
    "quota_exceeded",
    "quotaexceeded",
    "accountbalanceinsufficient",
    "account balance insufficient",
)


class GatewayError(Exception):
    """An error rendered as an OpenAI-style error object on the wire."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str = "invalid_request_error",
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = {"error": {"message": message, "type": error_type}}
        if code is not None:
            self.body["error"]["code"] = code
        self.headers = headers or {}
        super().__init__(message)


def invalid_request(message: str, code: str | None = None) -> GatewayError:
    return GatewayError(400, message, "invalid_request_error", code)


def provider_error(status_code: int, message: str) -> GatewayError:
    return GatewayError(status_code, message, "api_error", "provider_error")


def map_litellm_error(exc: Exception) -> GatewayError:
    """Map a LiteLLM exception to a GatewayError.

    M0: minimal pass-through of status_code + message. M3 extends this with the
    four-category mapping (QUOTA_EXHAUSTED etc.) — see ARCHITECTURE §4.4.
    """
    status = getattr(exc, "status_code", None)
    if not status or status < 100:
        status = 500
    message = getattr(exc, "message", None) or str(exc)
    return provider_error(status, message)


def classify_error(exc: Exception, provider: str | None = None) -> ErrorCategory:
    """Bucket an upstream exception into an :class:`ErrorCategory` (M3, R1).

    Works on both our own `GatewayError` (the common path, since adapters.py
    maps upstream exceptions before they reach the router) and raw LiteLLM
    exceptions (defensive). Classification is by status_code + error code +
    message substring — never status alone — so a generic 429 or 5xx is NEVER
    mistaken for quota exhaustion.

    `provider` (the candidate model prefix) is accepted for provider-specific
    rules (e.g. DeepSeek surfaces quota as 402); it is advisory only.
    """
    status: int | None = None
    code: str | None = None
    message = ""

    if isinstance(exc, GatewayError):
        status = exc.status_code
        err = exc.body.get("error", {})
        code = err.get("code")
        message = err.get("message", "")
    else:
        status = getattr(exc, "status_code", None) or getattr(
            exc, "exception_status_code", None
        )
        message = getattr(exc, "message", "") or str(exc)
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            code = (err or {}).get("code")
        resp = getattr(exc, "response", None)
        if resp is not None and hasattr(resp, "json"):
            try:
                j = resp.json()
                ec = (j.get("error") or {}).get("code")
                if ec:
                    code = ec
            except Exception:  # noqa: BLE001
                pass

    text = f"{code or ''} {message or ''}".lower()

    # 1) Auth / client 4xx (no retry, no quota mark) — 401/403 first.
    if status in (401, 403):
        return ErrorCategory.AUTH_ERROR
    # 2) Quota / billing exhaustion: 402 OR any quota token in code/message.
    if status == 402 or any(tok in text for tok in _QUOTA_TOKENS):
        return ErrorCategory.QUOTA_EXHAUSTED
    # 3) Generic rate limit (429, non-quota).
    if status == 429:
        return ErrorCategory.RATE_LIMITED
    # 4) Server errors / timeouts.
    if status is not None and status >= 500:
        return ErrorCategory.UPSTREAM_5XX
    if "timeout" in text or "timed out" in text:
        return ErrorCategory.TIMEOUT
    # 5) Remaining 4xx (400 bad request etc.) — not retryable, not quota.
    if status is not None and 400 <= status < 500:
        return ErrorCategory.AUTH_ERROR
    # 6) Anything else (unknown/null status) — safest to treat as upstream.
    return ErrorCategory.UPSTREAM_5XX


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)


def to_openai_error_body(exc: Exception) -> dict:
    """Return an OpenAI-style `{"error": {...}}` body for *any* exception.

    Used by the streaming SSE generator to emit a structured error event when the
    upstream fails mid-stream (US-M2-02) — instead of a silent disconnect.
    """
    if isinstance(exc, GatewayError):
        return exc.body
    return map_litellm_error(exc).body
