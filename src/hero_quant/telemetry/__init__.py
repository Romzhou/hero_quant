"""hero_quant.telemetry — OTel + structlog + heartbeat + circuit."""

from .circuit import CircuitBreaker
from .heartbeat import HeartbeatTimer, _set_emitter
from .otel import SessionTelemetryCoordinator, get_otel_mode

__all__ = ["SessionTelemetryCoordinator", "get_otel_mode", "HeartbeatTimer", "_set_emitter", "CircuitBreaker"]
