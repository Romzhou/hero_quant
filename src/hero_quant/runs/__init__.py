"""runs — PG 长任务队列（PR-201/202/203，批次14）。

对外只暴露契约里的入口：
- `RunQueue`（store.py）：入队/领取/心跳/收尾/取消/看门狗/查询；
- `StepLedger`（steps.py）：run_steps 幂等断点；
- `RunWorker`（worker.py）：消费者 + 协作式取消；
- `router`（api.py）：/v1/runs 与 SSE；
- `assert_no_queue_worker`（guards.py）：API 连接上的 worker GUC 守卫。

主控负责在 api/server.py 注册 router（前缀 `/v1/runs` 与 `/runs` 双注册），本包不自挂。
"""

from __future__ import annotations

from hero_quant.runs.api import router
from hero_quant.runs.guards import QueueWorkerOnApiConnection, assert_no_queue_worker
from hero_quant.runs.steps import StepLedger
from hero_quant.runs.store import (
    TERMINAL_STATUSES,
    RunNotFound,
    RunQueue,
    RunsError,
    RunsUnavailable,
    RunsValidationError,
)
from hero_quant.runs.worker import RunCancelled, RunContext, RunWorker, Step

__all__ = [
    "QueueWorkerOnApiConnection",
    "RunCancelled",
    "RunContext",
    "RunNotFound",
    "RunQueue",
    "RunWorker",
    "RunsError",
    "RunsUnavailable",
    "RunsValidationError",
    "Step",
    "StepLedger",
    "TERMINAL_STATUSES",
    "assert_no_queue_worker",
    "router",
]
