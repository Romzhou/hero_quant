"""hero_quant.telemetry — OTel + structlog backbone."""

from .otel import SessionTelemetryCoordinator, get_otel_mode

__all__ = ["SessionTelemetryCoordinator", "get_otel_mode"]
