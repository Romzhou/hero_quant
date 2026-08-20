"""Temporal Activity heartbeat 15s + heartbeatDetails 续跑占位 (Wave C5).

真实 Temporal 用法:
    from temporalio import activity
    @activity.defn
    async def run_backtest_activity(params):
        activity.heartbeat({"step": 1})
        details = activity.info().heartbeat_details  # 续跑恢复点

占位实现：
- HEARTBEAT_INTERVAL_SECONDS = 15
- heartbeat(details) / get_heartbeat_details() 内存占位
- HeartbeatHelper 线程/协程心跳循环占位
- heartbeatDetails 续跑：保存/恢复上次心跳上下文
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import Any, Dict, Optional

HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_INTERVAL = HEARTBEAT_INTERVAL_SECONDS  # alias
DEFAULT_HEARTBEAT_TIMEOUT = 30  # Temporal activity heartbeatTimeout 占位

# 线程/协程隔离的心跳上下文 — heartbeatDetails 续跑核心
_heartbeat_details_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_heartbeat_details", default=None
)
_thread_local = threading.local()


def _get_thread_details() -> Optional[Dict[str, Any]]:
    return getattr(_thread_local, "details", None)


def _set_thread_details(details: Optional[Dict[str, Any]]) -> None:
    _thread_local.details = details


def heartbeat(details: Dict[str, Any] | Any = None) -> None:
    """发送心跳 — 记录 heartbeatDetails 供续跑恢复.

    真实 Temporal 会调用 ``activity.heartbeat(details)`` 并受 heartbeatTimeout 约束。
    占位实现仅做内存记录 + ContextVar 同步，便于单测与本地续跑模拟。
    """
    # 归一化为 dict
    if details is None:
        payload: Dict[str, Any] = {"ts": time.time()}
    elif isinstance(details, dict):
        payload = dict(details)
        payload.setdefault("ts", time.time())
    else:
        payload = {"value": details, "ts": time.time()}

    # ContextVar + thread-local 双写，保证跨线程/协程可见
    try:
        _heartbeat_details_ctx.set(payload)
    except Exception:
        pass
    _set_thread_details(payload)

    # 真实 Temporal 分支占位 — 若在 Temporal worker 上下文中则透传
    try:
        from temporalio import activity as temporal_activity  # type: ignore

        # 仅在 activity 环境中有效，否则抛 RuntimeError，忽略
        temporal_activity.heartbeat(payload)  # type: ignore
    except Exception:
        pass


def get_heartbeat_details() -> Optional[Dict[str, Any]]:
    """获取上次心跳详情 — 用于 Activity 重试/续跑恢复.

    真实 Temporal: ``activity.info().heartbeat_details`` 或 ``activity.get_heartbeat_details()``
    """
    # 优先 ContextVar
    try:
        ctx_val = _heartbeat_details_ctx.get()
        if ctx_val is not None:
            return dict(ctx_val)
    except Exception:
        pass
    thread_val = _get_thread_details()
    if thread_val is not None:
        return dict(thread_val)

    # 尝试 Temporal 原生
    try:
        from temporalio import activity as temporal_activity  # type: ignore

        details = temporal_activity.info().heartbeat_details  # type: ignore
        if details:
            # Temporal 返回 tuple/list，取首个 dict
            if isinstance(details, (list, tuple)) and details:
                first = details[0]
                if isinstance(first, dict):
                    return dict(first)
                return {"value": first}
            if isinstance(details, dict):
                return dict(details)
    except Exception:
        pass
    return None


class HeartbeatHelper:
    """Activity 心跳辅助 — 每 15s 自动 heartbeat，支持续跑恢复点.

    用法:
        helper = HeartbeatHelper(interval=15)
        helper.start({"step": 0})
        # ... 长任务中 helper.heartbeat({"step": n})
        details = helper.get_details()  # 续跑时恢复
        helper.stop()
    """

    def __init__(self, interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
        self.interval = max(0.5, float(interval))
        self._details: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._async_task: Optional[asyncio.Task] = None

    def start(self, initial_details: Dict[str, Any] | None = None) -> None:
        self._details = dict(initial_details) if initial_details else {}
        self._stop.clear()
        heartbeat(self._details)
        # 后台线程每 interval 心跳一次（占位，无真实长任务也可用）
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="temporal-heartbeat")
            self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                heartbeat(self._details)
            except Exception:
                pass

    def heartbeat(self, details: Dict[str, Any] | Any) -> None:
        if isinstance(details, dict):
            self._details = dict(details)
        else:
            self._details = {"value": details}
        heartbeat(self._details)

    def get_details(self) -> Optional[Dict[str, Any]]:
        # 优先本实例，其次全局
        if self._details is not None:
            return dict(self._details)
        return get_heartbeat_details()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    # 异步变体占位
    async def astart(self, initial_details: Dict[str, Any] | None = None) -> None:
        self.start(initial_details)
        # 异步循环占位（可选）
        try:
            loop = asyncio.get_running_loop()
            self._async_task = loop.create_task(self._async_loop())
        except RuntimeError:
            pass

    async def _async_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.interval)
            try:
                heartbeat(self._details)
            except Exception:
                pass

    async def astop(self) -> None:
        self.stop()
        if self._async_task is not None:
            try:
                self._async_task.cancel()
            except Exception:
                pass


# 兼容别名 — 供外部 ``from hero_quant.checkpoint.temporal import HeartbeatTimer`` 误引用时友好提示
# （实际心跳由 HeartbeatHelper 提供）
try:
    HeartbeatTimer = HeartbeatHelper  # type: ignore
except Exception:
    pass


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_INTERVAL",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "heartbeat",
    "get_heartbeat_details",
    "HeartbeatHelper",
    "HeartbeatTimer",
]
