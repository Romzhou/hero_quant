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
# 中文注释：跨线程可见的共享存储 — ContextVar/thread-local 均线程隔离，需额外共享锁保护
_shared_details: Optional[Dict[str, Any]] = None
_shared_lock = threading.Lock()
_thread_local = threading.local()


def _get_thread_details() -> Optional[Dict[str, Any]]:
    """读取线程局部的心跳详情。"""
    return getattr(_thread_local, "details", None)


def _set_thread_details(details: Optional[Dict[str, Any]]) -> None:
    """写入线程局部的心跳详情。"""
    _thread_local.details = details


def clear_heartbeat_details() -> None:
    """清空三槽 heartbeatDetails（ContextVar + thread-local + 共享），防跨 activity 复用旧续跑点。"""
    try:
        _heartbeat_details_ctx.set(None)
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
        pass  # intentional offline-safe: temporal sidecar optional
    _set_thread_details(None)
    try:
        with _shared_lock:
            global _shared_details
            _shared_details = None
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)
        pass


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

    # ContextVar + thread-local + 共享锁保护的跨线程可见存储
    try:
        _heartbeat_details_ctx.set(payload)
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
        pass  # intentional offline-safe: temporal sidecar optional
    _set_thread_details(payload)
    # 中文注释：为跨线程可见，额外写入共享存储（线程安全）
    try:
        with _shared_lock:
            global _shared_details
            _shared_details = dict(payload)
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)
        pass

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
    # 中文注释：共享存储 — 跨线程可见
    try:
        with _shared_lock:
            if _shared_details is not None:
                return dict(_shared_details)
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)
        pass

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
        # 下限 0.5s，避免过密心跳对调度与网络造成压力；上限 30s 且不超过 heartbeatTimeout*0.8
        # 保证 heartbeat 必在 Temporal heartbeatTimeout 之前送达，防止 activity 被误判超时
        _upper = min(30.0, float(DEFAULT_HEARTBEAT_TIMEOUT) * 0.8)
        self.interval = min(max(0.5, float(interval)), _upper)
        self._details: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._async_task: Optional[asyncio.Task] = None

    def start(self, initial_details: Dict[str, Any] | None = None) -> None:
        """启动后台心跳线程，立即发送一次初始心跳。

        混合复用互斥：若异步任务在跑，同步 start 拒绝启动并抛错（调用方须先
        await astop()），不可悄悄并行双心跳，亦不可火忘取消后丢句柄。
        """
        # 中文：混合复用必须互斥——异步循环在跑时同步 start 拒绝启动（fail-closed），
        # 调用方须先 await astop()；不可悄悄并行（双心跳），亦不可火忘取消后丢句柄。
        _atask = self._async_task
        if _atask is not None and not _atask.done():
            raise RuntimeError("HeartbeatHelper.start: async heartbeat task still running; await astop() first")
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
        """停止后台心跳与异步任务。

        线程安全：绝不在事件循环线程内 join（阻塞 loop）；异步任务取消一律经
        loop.call_soon_threadsafe，不可跨线程直调 task.cancel()。
        """
        # 中文注释：同步 stop 也需取消异步任务，避免泄漏；取消一律经 loop 线程安全投递
        self._stop.set()
        if self._thread is not None:
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                _loop = None
            if _loop is None:
                self._thread.join(timeout=1.0)
                if self._thread.is_alive():
                    logger.warning("temporal 心跳线程未在 1s 内退出")
                else:
                    self._thread = None
            else:
                # 中文：在事件循环线程内不可 join（阻塞 loop）且不可丢句柄（否则泄漏/双心跳）；
                # 保留句柄交由 await astop() 回收
                logger.warning("stop() called on event-loop thread; skipping blocking join, use await astop()")
                return
        else:
            self._thread = None
        if self._async_task is not None:
            task = self._async_task
            # 中文：跨线程取消必须经 loop.call_soon_threadsafe，直调 task.cancel() 非线程安全
            try:
                if not task.done():
                    try:
                        t_loop = task.get_loop()
                    except Exception:
                        t_loop = None
                    if t_loop is not None and not t_loop.is_closed():
                        t_loop.call_soon_threadsafe(task.cancel)
                    else:
                        try:
                            task.cancel()
                        except RuntimeError as _exc:  # noqa: BLE001 窄化
                            logger.debug("stop 取消任务已完成: %s", _exc)
            except RuntimeError as _exc:  # noqa: BLE001 窄化
                logger.debug("stop 取消任务已完成: %s", _exc)
            except Exception as _exc:  # noqa: BLE001 兜底
                logger.debug("stop 取消异步任务失败: %s", _exc, exc_info=True)
            # 已尝试取消，不再置 None 由 astop 或后续清理；此处保留句柄以便 astop await
            # 若任务已被 cancel 且不在 loop 中，置 None 亦可；保留句柄更安全
            if task.done() or task.cancelled():
                self._async_task = None
        # 中文：停机即清三槽（sync/async 一致），避免旧续跑点泄漏到后一个 activity
        clear_heartbeat_details()

    # 异步变体占位
    async def astart(self, initial_details: Dict[str, Any] | None = None) -> None:
        """异步启动 — 仅起异步任务，不复用同步线程（避免双心跳）；重复启动先取消旧任务。"""
        # 中文注释：重启前需清 _stop，否则 stop 后立即退出
        self._stop.clear()
        self._details = dict(initial_details) if initial_details else {}
        try:
            heartbeat(self._details)
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
            pass  # intentional offline-safe: temporal sidecar optional
        # 中文：重复/混合启动不得孤儿化旧循环——先取消旧异步任务，并停同步线程，避免双心跳
        _old = self._async_task
        if _old is not None and not _old.done():
            _old.cancel()
            try:
                await _old
            except asyncio.CancelledError:
                pass
            except Exception as _exc:
                logger.debug("astart 回收旧任务: %s", _exc, exc_info=True)
        self._async_task = None
        if self._thread is not None:
            try:
                # 中文：同步线程不停会与异步循环双心跳；不可在 loop 线程阻塞 join，
                # 仅 signal _stop 并卸载 join 到执行器
                self._stop.set()
                await asyncio.to_thread(self._thread.join, 0.2)
            except Exception as _exc:
                logger.debug("astart 回收旧线程: %s", _exc, exc_info=True)
            if not self._thread.is_alive():
                self._thread = None
            self._stop.clear()
        # 异步循环占位（可选）
        try:
            loop = asyncio.get_running_loop()
            self._async_task = loop.create_task(self._async_loop())
        except RuntimeError as _exc:
            logger.warning("astart 无运行 loop，异步心跳未启动", exc_info=_exc)

    async def _async_loop(self) -> None:
        """异步心跳循环 — 与线程循环互补。"""
        # 中文注释：sleep 后需重检 _stop，避免 stop 后多发一次
        while not self._stop.is_set():
            await asyncio.sleep(self.interval)
            if self._stop.is_set():
                break
            try:
                heartbeat(self._details)
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
                pass  # intentional offline-safe: temporal sidecar optional

    async def astop(self) -> None:
        """异步停止 — 取消异步任务并回收线程（join 卸载到执行器，不阻塞事件循环）。"""
        self._stop.set()
        # 中文：join 不可在 loop 线程阻塞，卸载到执行器
        _thread = self._thread
        if _thread is not None:
            try:
                await asyncio.to_thread(_thread.join, 1.0)
                if _thread.is_alive():
                    logger.warning("temporal 心跳线程未在 1s 内退出")
            except Exception as _exc:
                logger.debug("astop 回收线程: %s", _exc, exc_info=True)
            self._thread = None
        # 中文：取消经 loop 线程安全投递并 await 回收（本协程即跑在任务 loop 上）
        task = self._async_task
        self._async_task = None
        if task is not None:
            try:
                if not task.done():
                    try:
                        task.get_loop().call_soon_threadsafe(task.cancel)
                    except Exception:
                        task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: temporal sidecar optional", exc_info=_exc)  # intentional: offline-safe: temporal sidecar optional
                pass  # intentional offline-safe: temporal sidecar optional
        # 中文：停机即清三槽，避免旧续跑点泄漏到后一个 activity
        clear_heartbeat_details()


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
    "clear_heartbeat_details",
    "get_heartbeat_details",
    "HeartbeatHelper",
    "HeartbeatTimer",
]
