"""Temporal 心跳与续跑占位。

职责：提供 Activity 心跳发送、heartbeatDetails 续跑恢复及后台心跳循环。
架构位置：`checkpoint` 侧车，被长任务与 Temporal Worker 集成引用。
关键设计：ContextVar + thread-local 双写保证跨协程/线程可见；15s 固定心跳间隔；`HeartbeatHelper` 封装线程/异步双循环，离线时静默兼容真实 `temporalio.activity.heartbeat`。
"""

from __future__ import annotations
import logging

import asyncio
import contextvars
import threading
import time
from typing import Any, Dict, Optional
logger = logging.getLogger("hero_quant.checkpoint.temporal")

HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_INTERVAL = HEARTBEAT_INTERVAL_SECONDS  # 兼容别名
DEFAULT_HEARTBEAT_TIMEOUT = 30  # Temporal activity heartbeatTimeout 占位

# 线程/协程隔离的心跳上下文 — heartbeatDetails 续跑核心（ContextVar 保证协程隔离）
_heartbeat_details_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_heartbeat_details", default=None
)
_thread_local = threading.local()


def _get_thread_details() -> Optional[Dict[str, Any]]:
    """读取线程局部的心跳详情。"""
    return getattr(_thread_local, "details", None)


def _set_thread_details(details: Optional[Dict[str, Any]]) -> None:
    """写入线程局部的心跳详情。"""
    _thread_local.details = details


def heartbeat(details: Dict[str, Any] | Any = None) -> None:
    """发送心跳 — 记录 heartbeatDetails 供重试/续跑恢复。"""
    # 归一化为 dict，便于后续序列化与 Temporal 透传
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
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
        pass  # intentional offline-safe: temporal sidecar optional
    _set_thread_details(payload)

    # 真实 Temporal 分支 — 若在 Activity 上下文中则透传，否则静默忽略
    try:
        from temporalio import activity as temporal_activity  # type: ignore

        # 仅在 activity 环境中有效，否则抛 RuntimeError，忽略
        temporal_activity.heartbeat(payload)  # type: ignore
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
        pass  # intentional offline-safe: temporal sidecar optional


def get_heartbeat_details() -> Optional[Dict[str, Any]]:
    """获取上次心跳详情，用于 Activity 重试/续跑恢复。"""
    # 优先 ContextVar（协程隔离更准确）
    try:
        ctx_val = _heartbeat_details_ctx.get()
        if ctx_val is not None:
            return dict(ctx_val)
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
        pass  # intentional offline-safe: temporal sidecar optional
    thread_val = _get_thread_details()
    if thread_val is not None:
        return dict(thread_val)

    # 回退：尝试 Temporal 原生 heartbeat_details
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
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
        pass  # intentional offline-safe: temporal sidecar optional
    return None


class HeartbeatHelper:
    """Activity 心跳辅助 — 每 15s 自动 heartbeat，支持续跑恢复点。"""

    def __init__(self, interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
        # 下限 0.5s，避免过密心跳对调度与网络造成压力
        self.interval = max(0.5, float(interval))
        self._details: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._async_task: Optional[asyncio.Task] = None

    def start(self, initial_details: Dict[str, Any] | None = None) -> None:
        """启动后台心跳线程，立即发送一次初始心跳。"""
        self._details = dict(initial_details) if initial_details else {}
        self._stop.clear()
        heartbeat(self._details)
        # 后台线程每 interval 心跳一次（daemon，避免阻塞进程退出）
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="temporal-heartbeat")
            self._thread.start()

    def _run_loop(self) -> None:
        """后台线程循环 — 定时透传最近一次 details。"""
        while not self._stop.wait(self.interval):
            try:
                heartbeat(self._details)
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
                pass  # intentional offline-safe: temporal sidecar optional

    def heartbeat(self, details: Dict[str, Any] | Any) -> None:
        """手动上报一次心跳并更新本地缓存。"""
        if isinstance(details, dict):
            self._details = dict(details)
        else:
            self._details = {"value": details}
        heartbeat(self._details)

    def get_details(self) -> Optional[Dict[str, Any]]:
        """获取最近心跳详情，优先本实例缓存。"""
        # 优先本实例，其次全局
        if self._details is not None:
            return dict(self._details)
        return get_heartbeat_details()

    def stop(self) -> None:
        """停止后台线程，最多等待 1s 保证资源回收。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._async_task = None

    # 异步变体占位
    async def astart(self, initial_details: Dict[str, Any] | None = None) -> None:
        """异步启动 — 仅起异步任务，不复用同步线程（避免双心跳）。"""
        self._details = dict(initial_details) if initial_details else {}
        try:
            heartbeat(self._details)
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
            pass  # intentional offline-safe: temporal sidecar optional
        # 异步循环占位（可选）
        try:
            loop = asyncio.get_running_loop()
            self._async_task = loop.create_task(self._async_loop())
        except RuntimeError:
            pass

    async def _async_loop(self) -> None:
        """异步心跳循环 — 与线程循环互补。"""
        while not self._stop.is_set():
            await asyncio.sleep(self.interval)
            try:
                heartbeat(self._details)
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
                pass  # intentional offline-safe: temporal sidecar optional

    async def astop(self) -> None:
        """异步停止 — 取消异步任务并回收线程。"""
        # save async task before stop() clears it
        _task = self._async_task
        self.stop()
        # restore if stop cleared it but not yet cancelled/awaited
        task = _task if _task is not None else None
        if task is not None:
            try:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
                pass  # intentional offline-safe: temporal sidecar optional
            self._async_task = None


# 兼容别名 — 历史导入 `HeartbeatTimer` 指向 HeartbeatHelper
try:
    HeartbeatTimer = HeartbeatHelper  # type: ignore
except Exception as _exc:
    logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
    pass  # intentional offline-safe: temporal sidecar optional


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_INTERVAL",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "heartbeat",
    "get_heartbeat_details",
    "HeartbeatHelper",
    "HeartbeatTimer",
]
