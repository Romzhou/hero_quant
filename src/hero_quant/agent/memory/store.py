"""Agent 会话缓冲存储：OrderedDict LRU。

职责：为 AgentLoop 提供进程内会话级 MemoryBuffer 的获取/淘汰（与
memory/store.py 的持久化 MemoryStore 区分：SessionBuffer 以 session_id 为 key，不落盘）。
关键设计：上限 MAX_SESSIONS=1000，命中时 move_to_end 保 LRU；淘汰最久未用
会话并显式 clear 释放引用；模块级 session_buffer 仅为离线兜底，生产经
inject_session_buffer(app) 注入 app.state.session_buffer。
app.state.memory_store 由 lifespan 装配持久化 hero_quant.memory.store.MemoryStore，两者不得混用。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from .buffer import MemoryBuffer

logger = logging.getLogger(__name__)

# 最大会话数限制，防止内存耗尽
MAX_SESSIONS = 1000


class SessionBuffer:
    """会话缓冲存储（带 LRU 淘汰策略，进程内、不落盘）。"""

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


# 离线兜底实例（生产应在启动时经 inject_session_buffer 注入 app.state）
session_buffer = SessionBuffer()


def get_session_buffer(request) -> SessionBuffer:
    """FastAPI Depends 工厂：优先 app.state.session_buffer，否则兜底全局实例。"""
    store = getattr(request.app.state, "session_buffer", None)
    if store is None:
        logger.warning("app.state.session_buffer missing, using fallback instance")
        return session_buffer
    return store


def inject_session_buffer(app, store: SessionBuffer | None = None) -> SessionBuffer:
    """lifespan 注入点：把 SessionBuffer 单例挂到 app.state.session_buffer。

    持久化记忆不走这里（见 server.py::_inject_r1_state_on_startup）。
    """
    bound = store or SessionBuffer()
    app.state.session_buffer = bound
    return bound
