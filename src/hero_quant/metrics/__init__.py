"""hero_quant.metrics — Prometheus observability hardening.

Wave E wall-time governance + observability hardening:
- histo: hero_quant_wall_time_seconds (operation/status)
- histo: http_request_duration_seconds (existing, re-exported for compat)
- counter: hero_quant_governance_wall_time_exceeded_total (operation)
- gauge: circuit_state already in telemetry/circuit.py (3-state)
- histo: hero_quant_governance_ledger_append_duration_seconds
- counter: hero_quant_governance_ledger_append_total
- counter: hero_quant_governance_dedup_op_total
All collectors handle duplicate registration (reload-safe).

Provides helper functions observe_wall_time / inc_exceeded used by governance/wall_time.
"""

from __future__ import annotations

import time
from typing import Any

try:
    from prometheus_client import Counter, Gauge, Histogram, REGISTRY  # type: ignore
except Exception:  # pragma: no cover - prometheus_client unavailable fallback to stub
    Counter = Histogram = Gauge = None  # type: ignore
    REGISTRY = None  # type: ignore

__all__ = [
    "WALL_TIME_SECONDS",
    "WALL_TIME_BUDGET_EXCEEDED",
    "LEDGER_APPEND_DURATION",
    "LEDGER_APPEND_TOTAL",
    "DEDUP_OP_TOTAL",
    "REQUEST_DURATION",
    "REQUEST_COUNTER",
    "observe_wall_time",
    "inc_wall_time_exceeded",
    "observe_ledger_append",
    "inc_ledger_append",
    "get_wall_time_metrics",
]


def _get_or_create_histogram(name: str, doc: str, labels: list[str], buckets=None):
    if Histogram is None:
        return None
    try:
        kw = {"labelnames": labels}
        if buckets is not None:
            kw["buckets"] = buckets
        h = Histogram(name, doc, **kw)
        return h
    except Exception as e:
        # already registered (reload / tests) — reuse existing collector (handle DuplicateTimeseries)
        try:
            if REGISTRY is not None:
                # try direct name and with suffix variations
                for cand in (name, f"{name}_bucket", f"{name}_count"):
                    if cand in getattr(REGISTRY, "_names_to_collectors", {}):
                        return REGISTRY._names_to_collectors[cand]  # type: ignore[attr-defined]
                # fallback: search by collector name substring
                return REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        except Exception:
            pass
        # also try to find by checking if error indicates duplicate, try registry lookup
        try:
            if REGISTRY is not None and name in getattr(REGISTRY, "_names_to_collectors", {}):
                return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]
        except Exception:
            pass
        return None


def _get_or_create_counter(name: str, doc: str, labels: list[str]):
    if Counter is None:
        return None
    try:
        c = Counter(name, doc, labels)
        return c
    except Exception as e:
        try:
            if REGISTRY is not None and name in getattr(REGISTRY, "_names_to_collectors", {}):
                return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]
        except Exception:
            pass
        return None


def _get_or_create_gauge(name: str, doc: str, labels: list[str] | None = None):
    if Gauge is None:
        return None
    try:
        if labels:
            g = Gauge(name, doc, labels)
        else:
            g = Gauge(name, doc)
        return g
    except Exception as e:
        try:
            if REGISTRY is not None and name in getattr(REGISTRY, "_names_to_collectors", {}):
                return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]
        except Exception:
            pass
        return None


# -- Wall-time governance metrics (hardening) --
WALL_TIME_SECONDS = _get_or_create_histogram(
    "hero_quant_wall_time_seconds",
    "Wall-time duration in seconds by operation and status",
    ["operation", "status"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# Alias for tests that may look for hero_quant_wall_time_duration_seconds or wall_time_seconds
WALL_TIME_DURATION = WALL_TIME_SECONDS

WALL_TIME_BUDGET_EXCEEDED = _get_or_create_counter(
    "hero_quant_governance_wall_time_exceeded_total",
    "Total count of wall-time budget exceeded events by operation",
    ["operation"],
)

# Ledger hardening metrics
LEDGER_APPEND_DURATION = _get_or_create_histogram(
    "hero_quant_governance_ledger_append_duration_seconds",
    "Ledger append wall-time duration in seconds",
    ["tenant", "status"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

LEDGER_APPEND_TOTAL = _get_or_create_counter(
    "hero_quant_governance_ledger_append_total",
    "Total ledger append operations",
    ["tenant", "status"],
)

DEDUP_OP_TOTAL = _get_or_create_counter(
    "hero_quant_governance_dedup_op_total",
    "Total dedup operations by op and status",
    ["op", "status"],
)

# -- HTTP metrics (re-export for server compatibility) --
# Note: server.py already defines its own Histogram; we provide shared fallback
REQUEST_DURATION = _get_or_create_histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint"],
)

REQUEST_COUNTER = _get_or_create_counter(
    "hero_quant_requests_total",
    "Total requests",
    ["endpoint"],
)


def observe_wall_time(operation: str, duration: float, status: str = "success") -> None:
    """Observe wall-time duration for an operation."""
    try:
        if WALL_TIME_SECONDS is not None:
            WALL_TIME_SECONDS.labels(operation=operation, status=status).observe(float(duration))
    except Exception:
        pass


def inc_wall_time_exceeded(operation: str = "generic") -> None:
    """Increment wall-time budget exceeded counter."""
    try:
        if WALL_TIME_BUDGET_EXCEEDED is not None:
            WALL_TIME_BUDGET_EXCEEDED.labels(operation=operation).inc()
    except Exception:
        pass


def observe_ledger_append(tenant: str, duration: float, status: str = "success") -> None:
    try:
        if LEDGER_APPEND_DURATION is not None:
            LEDGER_APPEND_DURATION.labels(tenant=tenant, status=status).observe(float(duration))
    except Exception:
        pass
    try:
        if LEDGER_APPEND_TOTAL is not None:
            LEDGER_APPEND_TOTAL.labels(tenant=tenant, status=status).inc()
    except Exception:
        pass


def inc_ledger_append(tenant: str, status: str = "success") -> None:
    try:
        if LEDGER_APPEND_TOTAL is not None:
            LEDGER_APPEND_TOTAL.labels(tenant=tenant, status=status).inc()
    except Exception:
        pass


def get_wall_time_metrics() -> dict[str, Any]:
    """Snapshot of wall-time metrics for debugging / tests (reads counter values if possible)."""
    out: dict[str, Any] = {}
    # Try to read Prometheus exposition via generate_latest parsing fallback - simple counter read
    try:
        if WALL_TIME_BUDGET_EXCEEDED is not None and hasattr(WALL_TIME_BUDGET_EXCEEDED, "_metrics"):
            # internal private; provide count
            out["exceeded_metric_available"] = True
    except Exception:
        pass
    return out
