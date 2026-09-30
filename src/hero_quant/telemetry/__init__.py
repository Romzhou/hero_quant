"""telemetry — 可观测性与韧性控制。

职责：提供 OTel 三档遥测、心跳探活、熔断/限流能力。
架构位置：`hero_quant.telemetry`，贯穿调用链路与后台探活。
关键设计：OTel 共享分级（disabled/shared/private）离线安全；心跳双看门狗 + Temporal 侧车；熔断双桶阈值驱动状态机。
"""

from .circuit import CircuitBreaker, DualBucketRateLimiter, TokenBucket
from .heartbeat import (
    HeartbeatTimer,
    _set_emitter,
    clear_probe_override,
    get_temporal_heartbeat_details,
    mark_probe_downstream_down,
    probe_temporal_sidecar,
    probe_temporal_sidecar_v2,
    sidecar_heartbeat_probe,
    temporal_heartbeat,
)
from .otel import SessionTelemetryCoordinator, durable_queue_depth, get_otel_mode

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
    "probe_temporal_sidecar_v2",
    "mark_probe_downstream_down",
    "clear_probe_override",
    "durable_queue_depth",
    "sidecar_heartbeat_probe",
]
