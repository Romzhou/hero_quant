"""checkpoint — 工作流检查点持久化与 Temporal 心跳。

职责：统一暴露 Postgres 检查点读写与 Temporal 心跳/续跑能力。
架构位置：`hero_quant.checkpoint`，被编排与调度层用于断点续跑。
关键设计：`memory://` 离线兜底 + 真实 Postgres 双路径；`thread_id` 三段式主键 + TTL 过期保障可恢复窗口。
"""

from .postgres import AsyncPostgresSaver, PostgresSaver, get_saver
from .temporal import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_INTERVAL_SECONDS,
    HeartbeatHelper,
    HeartbeatTimer,
    get_heartbeat_details,
    heartbeat,
)

__all__ = [
    "AsyncPostgresSaver",
    "PostgresSaver",
    "get_saver",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "HEARTBEAT_INTERVAL",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HeartbeatHelper",
    "HeartbeatTimer",
    "get_heartbeat_details",
    "heartbeat",
]
