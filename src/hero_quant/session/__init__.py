"""session — 会话持久化（W4 PR-131/132/134）。"""

from hero_quant.session.store import (
    MESSAGE_ROLES,
    RUN_STATUSES,
    SessionRunNotFound,
    SessionError,
    SessionNotFound,
    SessionStore,
    SessionStoreUnavailable,
    ToolCallNotFound,
    get_session_store,
    reset_session_store,
)

# 兼容别名（不是同名类定义）
RunNotFound = SessionRunNotFound

__all__ = [
    "MESSAGE_ROLES",
    "RUN_STATUSES",
    "SessionRunNotFound",
    "RunNotFound",
    "SessionError",
    "SessionNotFound",
    "SessionStore",
    "SessionStoreUnavailable",
    "ToolCallNotFound",
    "get_session_store",
    "reset_session_store",
]
