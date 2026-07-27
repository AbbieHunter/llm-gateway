"""PII detection / redaction guardrails (M4, US-M4-05 / R8).

Compliance-first, default OFF. Two independent surfaces:

- Inbound (request to upstream): when enabled, detected PII in message content
  is redacted (or only detected, per GUARDRAILS_INBOUND_MODE) BEFORE the request
  leaves the gateway. This is the safe default — it minimises what we send to a
  third-party model.
- Outbound (model response): a SEPARATE, stricter switch GUARDRAILS_OUTBOUND_MASK
  (default OFF). Masking a model's reply can destroy legitimate output (e.g. a
  generated phone number), so it is off unless explicitly opted in.

Hard rules (R8):
- Never persist detected raw PII. We only ever read/redact in transit.
- Streaming responses: only inbound redaction applies; outbound masking on a
  token stream is best-effort and intentionally NOT implemented (documented
  non-goal) — a streamed PII span can split across chunks and would be unsafe to
  mask mid-flight.
- Local regex rules by default; an external audit API can be plugged in later.
"""
from __future__ import annotations

import re

from app import config

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")           # mainland CN mobile
ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")             # 18-digit CN ID card


def _redact(text: str) -> str:
    text = EMAIL_RE.sub("[REDACTED-EMAIL]", text)
    text = PHONE_RE.sub("[REDACTED-PHONE]", text)
    text = ID_RE.sub("[REDACTED-ID]", text)
    return text


def redact_text(text: str, mode: str = "redact") -> str:
    """Redact PII when `mode == "redact"`; otherwise return unchanged (detect)."""
    if mode == "detect":
        return text
    return _redact(text)


def scan_text(text: str) -> list[str]:
    """Return detected PII snippets (for audit logging only — never stored)."""
    found: list[str] = []
    found += EMAIL_RE.findall(text)
    found += PHONE_RE.findall(text)
    found += ID_RE.findall(text)
    return found


def inbound_enabled() -> bool:
    return config.GUARDRAILS_ENABLED


def outbound_mask_enabled() -> bool:
    return config.GUARDRAILS_ENABLED and config.GUARDRAILS_OUTBOUND_MASK


def redact_message_content(content: object) -> object:
    """Redact PII in a message content (str or multimodal list). Inbound use."""
    if isinstance(content, str):
        return redact_text(content, config.GUARDRAILS_INBOUND_MODE)
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                item = {**item, "text": redact_text(item["text"], config.GUARDRAILS_INBOUND_MODE)}
            out.append(item)
        return out
    return content


def redact_response_content(resp: object) -> object:
    """Mask PII in a non-stream response's choices[].message.content (outbound).

    Mutates objects in place; returns the (possibly mutated) response. No-ops on
    None / unrecognised shapes.
    """
    if resp is None:
        return resp
    if hasattr(resp, "choices"):
        for ch in resp.choices:
            msg = getattr(ch, "message", None)
            if msg is not None and isinstance(getattr(msg, "content", None), str):
                msg.content = _redact(msg.content)
        return resp
    if isinstance(resp, dict):
        for ch in resp.get("choices", []):
            msg = ch.get("message") if isinstance(ch, dict) else None
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                msg["content"] = _redact(msg["content"])
        return resp
    return resp
