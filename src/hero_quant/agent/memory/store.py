"""Agent 会话记忆存储：OrderedDict LRU。

职责：为 AgentLoop 提供进程内会话级 MemoryBuffer 的获取/淘汰（与
memory/store.py 的持久化 MemoryStore 区分：此处 key 为 session_id，不落盘）。
关键设计：上限 MAX_SESSIONS=1000，命中时 move_to_end 保 LRU；淘汰最久未用
会话并显式 clear 释放引用；模块级 memory_store 仅为离线兜底，生产经
inject_memory_store(app) 注入 app.state.memory_store。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from .buffer import MemoryBuffer

logger = logging.getLogger(__name__)

# 最大会话数限制，防止内存耗尽
MAX_SESSIONS = 1000


class MemoryStore:
    """会话记忆存储（带 LRU 淘汰策略）。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS):
        self._buffers: OrderedDict[int, MemoryBuffer] = OrderedDict()
        self._max_sessions = max_sessions
        # 并发一致性：OrderedDict 复合操作需加锁（C 波锁纪律）
        self._lock = threading.RLock()

    def get_buffer(self, session_id: int, max_turns: int = 20) -> MemoryBuffer:
        """获取或创建记忆缓冲区"""
        with self._lock:
            if session_id in self._buffers:
                self._buffers.move_to_end(session_id)
                return self._buffers[session_id]

            if len(self._buffers) >= self._max_sessions:
                evicted_id, evicted_buffer = self._buffers.popitem(last=False)
                if evicted_buffer is not None:
                    evicted_buffer.clear()
                logger.warning("memory store full, evicted session: %s", evicted_id)

            buffer = MemoryBuffer(max_turns=max_turns)
            self._buffers[session_id] = buffer
            return buffer

    def save_buffer(self, session_id: int, buffer: MemoryBuffer):
        """保存记忆缓冲区（受 MAX_SESSIONS 约束）"""
        with self._lock:
            # 若为新会话且已满则先淘汰最旧，避免无界增长
            if session_id not in self._buffers and len(self._buffers) >= self._max_sessions:
                evicted_id, evicted_buffer = self._buffers.popitem(last=False)
                if evicted_buffer is not None:
                    evicted_buffer.clear()
                logger.warning("memory store full, evicted session: %s", evicted_id)
            old = self._buffers.get(session_id)
            if old is not None and old is not buffer:
                old.clear()
            self._buffers[session_id] = buffer
            self._buffers.move_to_end(session_id)

    def clear_buffer(self, session_id: int):
        """清除会话记忆"""
        with self._lock:
            if session_id in self._buffers:
                self._buffers[session_id].clear()

    def delete_buffer(self, session_id: int):
        """删除缓冲区"""
        with self._lock:
            if session_id in self._buffers:
                buffer = self._buffers.pop(session_id)
                if buffer is not None:
                    buffer.clear()

    def has_buffer(self, session_id: int) -> bool:
        """检查是否有缓冲"""
        with self._lock:
            return session_id in self._buffers

    @property
    def size(self) -> int:
        """当前缓存的会话数"""
        with self._lock:
            return len(self._buffers)


# 离线兜底实例（生产应在启动时经 inject_memory_store 注入 app.state）
memory_store = MemoryStore()


def get_memory_store(request) -> MemoryStore:
    """FastAPI Depends 工厂：优先 app.state.memory_store，否则兜底全局实例。"""
    store = getattr(request.app.state, "memory_store", None)
    if store is None:
        logger.warning("app.state.memory_store missing, using fallback instance")
        return memory_store
    return store


def inject_memory_store(app, store: MemoryStore | None = None) -> MemoryStore:
    """lifespan 注入点：把 MemoryStore 单例挂到 app.state.memory_store。

    server.py 正被他轨修改时可仅调用此函数而不改 lifespan 本体。
    """
    bound = store or MemoryStore()
    app.state.memory_store = bound
    return bound
