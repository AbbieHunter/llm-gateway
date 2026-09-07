"""Named OpenAI-compatible upstreams (multi-relay).

Default `openai/` candidates share process-wide `OPENAI_API_KEY` /
`OPENAI_API_BASE`. A second (or third) compatible relay needs its own base URL
and key without colliding — use a distinct model prefix plus env:

    UPSTREAM_RELAY_B_API_KEY=...
    UPSTREAM_RELAY_B_API_BASE=https://relay-b.example/v1
    UPSTREAM_RELAY_B_KIND=openai   # optional; default openai

Route candidates then use `relay_b/qwen-plus`. Before LiteLLM, we rewrite to
`openai/qwen-plus` and pass `api_key` / `api_base` explicitly.

Native prefixes (`openai`, `deepseek`, `anthropic`, …) with no `UPSTREAM_*`
continue to rely on LiteLLM's OS-env convention.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.core.errors import GatewayError


@dataclass(frozen=True)
class NamedUpstream:
    prefix: str
    token: str
    api_key: str | None
    api_base: str | None
    kind: str


def env_token(prefix: str) -> str:
    """Map model prefix `relay_b` / `relay-b` -> env token `RELAY_B`."""
    return prefix.strip().replace("-", "_").upper()


def lookup(prefix: str) -> NamedUpstream | None:
    """Return named upstream if `UPSTREAM_{TOKEN}_API_BASE` or `_API_KEY` is set."""
    if not prefix:
        return None
    token = env_token(prefix)
    key = os.getenv(f"UPSTREAM_{token}_API_KEY") or None
    base = os.getenv(f"UPSTREAM_{token}_API_BASE") or None
    if not key and not base:
        return None
    kind = (os.getenv(f"UPSTREAM_{token}_KIND") or "openai").strip().lower() or "openai"
    return NamedUpstream(
        prefix=prefix,
        token=token,
        api_key=key,
        api_base=base,
        kind=kind,
    )


def prepare_litellm_model(model: str) -> tuple[str, dict]:
    """Map a gateway candidate model string to LiteLLM `(model, kwargs)`.

    Strips mock query controls (`?__quota=1`) before prefix lookup; the
    returned model string never includes the query part for named upstreams.
    """
    base, _, _qs = model.partition("?")
    if "/" not in base:
        return model, {}

    prefix, rest = base.split("/", 1)
    upstream = lookup(prefix)
    if upstream is None:
        return model, {}

    if upstream.kind == "openai":
        if not upstream.api_base:
            raise GatewayError(
                503,
                f"named upstream '{prefix}' is configured but "
                f"UPSTREAM_{upstream.token}_API_BASE is missing "
                f"(refusing to fall through to the default OpenAI endpoint)",
                "api_error",
                "upstream_misconfigured",
            )
        kwargs: dict = {"api_base": upstream.api_base}
        if upstream.api_key:
            kwargs["api_key"] = upstream.api_key
        return f"openai/{rest}", kwargs

    raise GatewayError(
        503,
        f"named upstream '{prefix}' has unsupported KIND={upstream.kind!r} "
        f"(only 'openai' is supported for UPSTREAM_* relays)",
        "api_error",
        "upstream_misconfigured",
    )
