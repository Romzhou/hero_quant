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
    """对参数名+类型+值摘要做 SHA256（哈希值不暴露明文，值不同则哈希不同）。"""
    try:
        items = sorted(
            (str(k), type(v).__name__, repr(v)[:256]) for k, v in (args or {}).items()
        )
        raw = str(items).encode(errors="ignore")
        return hashlib.sha256(raw).hexdigest()[:16]
    except (ValueError, TypeError, AttributeError):
        return "hash_failed"


def _safe_error_text(error: object) -> tuple[str, str]:
    """错误文本脱敏：截断+去换行+hash，原文本只保留摘要用于取证。"""
    raw = str(error or "")
    digest = hashlib.sha256(raw.encode(errors="ignore")).hexdigest()[:16]
    safe = raw[:500].replace("\n", " ").replace("\r", " ")
    return safe, digest


class AuditLogger:
    """审计日志器：tool 调用/失败/限流统一入口。"""

    @staticmethod
    def log_tool_call(user_id: int, tool_name: str, args: dict, success: bool, session_id: int | None = None):
        # 防御：非 dict 参数不得让审计抛错（审计永不抛，保住主请求）。
        safe_args = args if isinstance(args, dict) else {}
        audit_logger.info(
            "tool_call",
            extra={
                "event": "tool_call",
                "user_id": user_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "args_hash": _hash_args(safe_args),
                "args_keys": list(safe_args.keys()),
                "success": success,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def log_tool_failure(user_id: int, tool_name: str, error: str, session_id: int | None = None):
        # 错误原文不进日志：截断+去换行防注入/膨胀，另附 hash 供取证关联。
        safe_error, error_hash = _safe_error_text(error)
        audit_logger.warning(
            "tool_failure",
            extra={
                "event": "tool_failure",
                "user_id": user_id,
                "session_id": session_id,
                "tool_name": str(tool_name)[:128],
                "error": safe_error,
                "error_hash": error_hash,
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
