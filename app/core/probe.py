"""Provider quota auto-recovery probe (M3, US-M3-04, R3).

A background asyncio task (started on FastAPI startup, cancelled on shutdown)
periodically scans providers flagged `quota_exhausted` in Redis and sends a tiny
completion to see whether the budget has refilled. On success the mark is cleared
back to `healthy`; on failure (including network errors) the mark is KEPT — a
network error must never be flipped to `down`, since quota and reachability are
orthogonal (ARCHITECTURE §4.5 red line).

Failure backoff (R3): after a failed probe the next attempt for that provider is
delayed by an exponentially growing interval (doubles each failure, capped at
PROBE_COOLDOWN_CAP_SEC) so we don't burn a real budget probing a still-exhausted
provider.

In tests (MOCK_PROVIDER=1) the probe targets `{provider_id}/echo`, which the mock
adapter answers successfully, so a flagged provider auto-recovers. In production
the same call would use the real provider (operator should rely on real traffic
recovery for providers without a usable echo model; the probe is best-effort).
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.config import PROBE_COOLDOWN_CAP_SEC, PROBE_INTERVAL_SEC
from app.core.adapters import chat_completion
from app.core.health import QUOTA_EXHAUSTED, list_flagged, set_status
from app.core.redis_client import is_configured

_logger = logging.getLogger("gateway")

_task: asyncio.Task | None = None
_cooldown: dict[str, int] = {}  # provider_id -> current backoff interval (s)
_next_try: dict[str, float] = {}  # provider_id -> earliest monotonic ts to retry


async def _probe_provider(provider_id: str) -> None:
    model = f"{provider_id}/echo"
    try:
        await chat_completion(
            model, [{"role": "user", "content": "hi"}], stream=False
        )
        # Recovered: clear the mark and reset any backoff.
        await set_status(provider_id, "healthy")
        _cooldown.pop(provider_id, None)
        _next_try.pop(provider_id, None)
        _logger.info("probe: provider %s recovered -> healthy", provider_id)
    except Exception as exc:  # noqa: BLE001
        # Keep the quota_exhausted mark; never flip to down on a network error.
        cur = _cooldown.get(provider_id, PROBE_INTERVAL_SEC)
        new = min(PROBE_COOLDOWN_CAP_SEC, cur * 2)
        _cooldown[provider_id] = new
        _next_try[provider_id] = time.monotonic() + new
        _logger.info(
            "probe: provider %s still unavailable (%s); next probe in %ss",
            provider_id,
            exc,
            new,
        )


async def _probe_once() -> None:
    if not is_configured():
        return
    flagged = await list_flagged()
    now = time.monotonic()
    for item in flagged:
        if item["status"] != QUOTA_EXHAUSTED:
            continue
        pid = item["id"]
        nt = _next_try.get(pid)
        if nt is not None and now < nt:
            continue
        try:
            await _probe_provider(pid)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("probe: unexpected error probing %s: %s", pid, exc)


async def _run() -> None:
    while True:
        try:
            await _probe_once()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("probe: sweep error: %s", exc)
        await asyncio.sleep(PROBE_INTERVAL_SEC)


def start_probe_loop() -> None:
    """Start the probe background task (no-op if Redis is not configured)."""
    global _task
    if not is_configured():
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run())


def stop_probe_loop() -> None:
    """Cancel the probe background task (idempotent)."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
