"""runs.guards — 队列连接上的 RLS/GUC 守卫（PR-201）。

背景（docs/contracts/batch-14.md §2.4）：worker 跨租户领取依赖事务级 GUC
`app.queue_worker='on'`。这条 GUC 只允许出现在 **领取事务内**（SET LOCAL）。
一旦它残留在池化连接上，下一个复用该连接的 API 请求就会拿到跨租户可见性 —— 静默
数据越权。本模块提供一个显式故障：宁可 503，也不带着跨租户可见性继续服务。
"""

from __future__ import annotations

from typing import Any

from hero_quant.db.rows import fetch_one

_QUEUE_WORKER_GUC_SQL = "SELECT current_setting('app.queue_worker', true) AS queue_worker"


class QueueWorkerOnApiConnection(RuntimeError):
    """API 连接上检测到 worker GUC：池被混用或用了会话级 SET，必须立刻停。

    error/reason 供 API 映射成稳定的机器可读字段（绝不把连接串或口令带进响应）。
    """

    error = "queue_worker_guc_leaked"
    reason = "queue_worker_on_api_connection"

    def __init__(self, detail: str = "") -> None:
        message = f"{self.error}: {self.reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error, "reason": self.reason}


def queue_worker_guc(conn: Any) -> str | None:
    """读当前连接的 `app.queue_worker`；未设置返回 None（读不到也返回 None）。

    `current_setting(name, missing_ok=true)` 在 GUC 从未设置时返回 NULL，而 SET LOCAL
    在事务结束后会把值还原成空串 —— 两种都表示「没有 worker 权限」，调用方必须按空值处理。
    """
    row = fetch_one(conn, _QUEUE_WORKER_GUC_SQL)
    if not isinstance(row, dict):
        return None
    value = row.get("queue_worker")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def assert_no_queue_worker(conn: Any) -> None:
    """API 侧每次取到连接后调用；GUC 非空即抛 QueueWorkerOnApiConnection。

    为什么不是简单忽略：SET LOCAL 失效后读到的是空串，说明这条连接干净；若读到
    `on` 说明有会话级 SET 残留（或池被 worker/API 混用），此时任何查询都可能跨租户。
    静默继续 = 把越权当正常，故直接抛。
    """
    leaked = queue_worker_guc(conn)
    if leaked:
        raise QueueWorkerOnApiConnection(f"app.queue_worker={leaked!r}")


__all__ = ["QueueWorkerOnApiConnection", "assert_no_queue_worker", "queue_worker_guc"]
