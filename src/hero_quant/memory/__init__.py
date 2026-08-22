"""记忆子包：持久化、层次路由与生命周期管理的统一入口。

职责：为 Agent / Graph 层提供记忆的写入、检索与回收能力。
架构位置：上游为 Agent 工具与上下文，下游落盘到文件目录 + SQLite FTS5 + 可选 pgvector。
对外导出 ``MemoryStore``（双存储与去重）、``MemoryHierarchy``（目录路由）、``MemoryLifecycle``（衰减与 GC）。
"""

from .hierarchy import CATEGORIES, MemoryHierarchy
from .lifecycle import (
    ARCHIVE_THRESHOLD,
    DELETE_THRESHOLD,
    MIN_AGE_DAYS,
    MemoryLifecycle,
    compute_importance,
)
from .store import MemoryStore, PgVectorSidecar, get_vector_dim, is_pgvector_configured

__all__ = [
    "MemoryStore",
    "PgVectorSidecar",
    "MemoryHierarchy",
    "MemoryLifecycle",
    "CATEGORIES",
    "ARCHIVE_THRESHOLD",
    "DELETE_THRESHOLD",
    "MIN_AGE_DAYS",
    "compute_importance",
    "is_pgvector_configured",
    "get_vector_dim",
]
