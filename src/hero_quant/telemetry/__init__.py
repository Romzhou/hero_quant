"""hero_quant.telemetry — OTel + structlog + heartbeat + circuit."""

from .circuit import CircuitBreaker, DualBucketRateLimiter, TokenBucket
from .heartbeat import (
    HeartbeatTimer,
    _set_emitter,
    get_temporal_heartbeat_details,
    probe_temporal_sidecar,
    sidecar_heartbeat_probe,
    temporal_heartbeat,
)
from .otel import SessionTelemetryCoordinator, get_otel_mode

__all__ = [
    "SessionTelemetryCoordinator",
    "get_otel_mode",
    "HeartbeatTimer",
    "_set_emitter",
    "CircuitBreaker",
    "DualBucketRateLimiter",
    "TokenBucket",
    "temporal_heartbeat",
    "get_temporal_heartbeat_details",
    "probe_temporal_sidecar",
    "sidecar_heartbeat_probe",
]
