"""Agent 短期记忆包：会话级环形缓冲 + LRU 会话存储。

职责：为 loop.py 的 memory_store 鸭式持有提供轻量会话记忆（与 memory/store.py
的持久化 MemoryStore 区分：此处为进程内短期缓冲，不落盘）。
架构位置：agent 层短期记忆，被 AgentLoop.inject/_writeback 经鸭式接口调用。
关键设计：buffer 用 deque(maxlen=max_turns*2) O(1) 自动裁剪；store 用
OrderedDict LRU，上限 MAX_SESSIONS=1000，淘汰时显式 clear 释放引用。
"""

from .buffer import Message, MemoryBuffer
from .store import MAX_SESSIONS, MemoryStore, get_memory_store, memory_store

__all__ = ["MAX_SESSIONS", "MemoryBuffer", "MemoryStore", "Message", "get_memory_store", "memory_store"]
