"""Observability metrics (M4, US-M4-04 / R7).

Zero-dependency Prometheus exposition. We deliberately do NOT pull in
`prometheus-client` here: it is pure-Python and 3.13-compatible, but the project
keeps a strict "no new dependency unless needed" discipline, and a tiny
self-contained exposition covers exactly the §4.8 surface (request count, error
rate, p95 latency, provider health/quota, token throughput, cache hit rate,
quota-mislabel count). If a richer client is ever wanted, `render()` is the only
function to swap.

Design rules (R7):
- Cheap counter/gauge increments only on the hot path — no heavy computation.
- Only aggregate counts are exposed. No VK id, no PII, no provider secret ever
  enters a metric label or value.
- Latency is tracked with a bounded reservoir (last N samples) per label set so
  memory stays flat; p95 is computed from that reservoir.
"""
from __future__ import annotations

import threading

from app.config import METRICS_ENABLED

_lock = threading.Lock()
_counters: dict[tuple, float] = {}          # (name, frozenset(labels)) -> value
_gauges: dict[tuple, float] = {}            # (name, frozenset(labels)) -> value
_latency: dict[tuple, list[float]] = {}     # (name, frozenset(labels)) -> reservoir
_provider_status: dict[str, str] = {}       # provider_id -> status string

_RESERVOIR = 1024


def _key(name: str, labels: dict[str, str] | None) -> tuple:
    return (name, frozenset((labels or {}).items()))


def inc_counter(name: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
    if not METRICS_ENABLED:
        return
    key = _key(name, labels)
    with _lock:
        _counters[key] = _counters.get(key, 0.0) + amount


def set_gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    if not METRICS_ENABLED:
        return
    key = _key(name, labels)
    with _lock:
        _gauges[key] = value


def observe_latency(name: str, seconds: float, labels: dict[str, str] | None = None) -> None:
    if not METRICS_ENABLED:
        return
    key = _key(name, labels)
    with _lock:
        buf = _latency.setdefault(key, [])
        buf.append(seconds)
        if len(buf) > _RESERVOIR:
            # Drop oldest to keep the reservoir bounded.
            del buf[: len(buf) - _RESERVOIR]


def record_provider_status(provider_id: str, status: str) -> None:
    """Mirror the Redis provider-status vocabulary into a gauge set (R7)."""
    if not METRICS_ENABLED:
        return
    with _lock:
        _provider_status[provider_id] = status


def _p95(buf: list[float]) -> float:
    if not buf:
        return 0.0
    s = sorted(buf)
    idx = max(0, int(round(0.95 * (len(s) - 1))))
    return s[idx]


def render() -> str:
    """Render all metrics in the Prometheus text exposition format."""
    if not METRICS_ENABLED:
        return "# metrics disabled (METRICS_ENABLED=0)\n"
    lines: list[str] = []

    # Counters
    for (name, labels), value in sorted(_counters.items()):
        _emit(lines, name, "counter", labels, value)
    # Gauges (excluding provider status which is emitted separately below)
    for (name, labels), value in sorted(_gauges.items()):
        _emit(lines, name, "gauge", labels, value)
    # Latency summaries
    for (name, labels), buf in sorted(_latency.items()):
        label_str = _label_str(labels)
        cnt = len(buf)
        total = sum(buf)
        lines.append(f"# HELP {name} {name}")
        lines.append(f"# TYPE {name} summary")
        lines.append(f"{name}_count{label_str} {cnt}")
        lines.append(f"{name}_sum{label_str} {total:.6f}")
        lines.append(f"{name}_p95{label_str} {_p95(buf):.6f}")
    # Provider status as a gauge set: 1 for the current status, 0 otherwise.
    levels = {"healthy": 0, "degraded": 1, "down": 2, "quota_exhausted": 3}
    with _lock:
        providers = dict(_provider_status)
    lines.append("# HELP gateway_provider_status Current provider runtime status level")
    lines.append("# TYPE gateway_provider_status gauge")
    for pid, st in sorted(providers.items()):
        for lvl, lvl_val in levels.items():
            active = 1 if lvl == st else 0
            lines.append(f'gateway_provider_status{{provider="{pid}",status="{lvl}"}} {active}')
    return "\n".join(lines) + "\n"


def _label_str(labels: frozenset) -> str:
    if not labels:
        return ""
    parts = ",".join(f'{k}="{v}"' for k, v in sorted(labels))
    return "{" + parts + "}"


def _emit(lines: list[str], name: str, mtype: str, labels: frozenset, value: float) -> None:
    label_str = _label_str(labels)
    lines.append(f"# HELP {name} {name}")
    lines.append(f"# TYPE {name} {mtype}")
    lines.append(f"{name}{label_str} {value}")
