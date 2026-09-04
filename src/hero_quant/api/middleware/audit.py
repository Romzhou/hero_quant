"""api.middleware.audit — 结构化审计日志。

职责：记录 tool 调用/失败/限流命中等关键事件，仅记参数名与哈希，不记明文。
参考 skills/fastapi-agent-module-skill/references/audit.py。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

audit_logger = logging.getLogger("api.audit")


def _hash_args(args: dict) -> str:
    """对参数名+类型做 SHA256（不暴露明文）。"""
    try:
        meta = {k: type(v).__name__ for k, v in (args or {}).items()}
        raw = str(sorted(meta.items())).encode()
        return hashlib.sha256(raw).hexdigest()[:16]
    except (ValueError, TypeError, AttributeError):
        return "hash_failed"


class AuditLogger:
    """审计日志器：tool 调用/失败/限流统一入口。"""

    @staticmethod
    def log_tool_call(user_id: int, tool_name: str, args: dict, success: bool, session_id: int | None = None):
        audit_logger.info(
            "tool_call",
            extra={
                "event": "tool_call",
                "user_id": user_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "args_hash": _hash_args(args or {}),
                "args_keys": list((args or {}).keys()),
                "success": success,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def log_tool_failure(user_id: int, tool_name: str, error: str, session_id: int | None = None):
        audit_logger.warning(
            "tool_failure",
            extra={
                "event": "tool_failure",
                "user_id": user_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def log_rate_limit_hit(user_id: int, endpoint: str):
        audit_logger.warning(
            "rate_limit_hit",
            extra={
                "event": "rate_limit_hit",
                "user_id": user_id,
                "endpoint": endpoint,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


__all__ = ["AuditLogger", "audit_logger"]
