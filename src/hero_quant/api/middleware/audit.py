"""api.middleware.audit — 结构化审计日志。

职责：记录 tool 调用/失败/限流命中等关键事件，仅记参数名与哈希，不记明文。
参考 skills/fastapi-agent-module-skill/references/audit.py。
"""

from __future__ import annotations

import hashlib
import logging
import re
import reprlib
from datetime import datetime, timezone

audit_logger = logging.getLogger("api.audit")

# 日志令牌上限：防注入/防膨胀（tool_name 128，endpoint 256，args_keys 64 个）。
_MAX_TOOL_NAME_LEN = 128
_MAX_ENDPOINT_LEN = 256
_MAX_ARGS_KEYS = 64
_MAX_REPR_LEN = 256

_repr = reprlib.Repr()
_repr.maxstring = _MAX_REPR_LEN
_repr.maxlevel = 4
_repr.maxtuple = 8
_repr.maxlist = 8
_repr.maxdict = 8
_repr.maxset = 8


def _bounded_repr(v: object, limit: int = _MAX_REPR_LEN) -> str:
    """有界 repr：大字符串先截断再取值，reprlib 兜底；永不抛错。"""
    try:
        if isinstance(v, str):
            s = v if len(v) <= limit else v[:limit] + "..."
        else:
            s = _repr.repr(v)
    except Exception:
        return "<unrepresentable>"
    try:
        return s[:limit]
    except Exception:
        return "<unrepresentable>"


def _sanitize_token(value: object, limit: int = _MAX_TOOL_NAME_LEN) -> str:
    """日志令牌净化：转 str 失败回退占位；截断+去换行防注入/膨胀。"""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unrepresentable>"
    try:
        return s[:limit].replace("\n", " ").replace("\r", " ")
    except Exception:
        return "<unrepresentable>"


def _hash_args(args: dict) -> str:
    """对参数名+类型+值摘要做 SHA256（哈希值不暴露明文，值不同则哈希不同）。

    确定性 fail-open 契约：hostile __repr__（抛错）统一归一化为固定占位
    "<unrepresentable>" 再哈希，绝不混入 id()/内存地址等非确定性成分；
    同一输入两次调用结果相等，且永不抛错。
    """
    try:
        parts = []
        for k, v in (args or {}).items():
            try:
                parts.append((_sanitize_token(k), type(v).__name__, _bounded_repr(v)))
            except Exception:
                parts.append(("<unrepresentable-key>", "unknown", "<unrepresentable>"))
        # 确定性归一化：剥离 repr 中偶发的内存地址（0x...）等非确定性片段。
        normed: list[tuple[str, str, str]] = []
        for kk, tt, vv in parts:
            try:
                vv = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", vv)
            except Exception:
                vv = "<unrepresentable>"
            normed.append((kk, tt, vv))
        items = sorted(normed)
        raw = str(items).encode(errors="ignore")
        return hashlib.sha256(raw).hexdigest()[:16]
    except Exception:
        return "hash_failed"


def _safe_error_text(error: object) -> tuple[str, str]:
    """错误文本脱敏：截断+去换行+hash，原文本只保留摘要用于取证。"""
    try:
        raw = str(error) if error is not None else ""
    except Exception:
        raw = "<unrepresentable error>"
    try:
        digest = hashlib.sha256(raw.encode(errors="ignore")).hexdigest()[:16]
    except Exception:
        digest = "hash_failed"
    try:
        safe = raw[:500].replace("\n", " ").replace("\r", " ")
    except Exception:
        safe = "<unrepresentable error>"
    return safe, digest


class AuditLogger:
    """审计日志器：tool 调用/失败/限流统一入口。"""

    @staticmethod
    def log_tool_call(user_id: int, tool_name: str, args: dict, success: bool, session_id: int | None = None):
        # 防御：非 dict 参数不得让审计抛错（审计永不抛，保住主请求）。
        try:
            safe_args = args if isinstance(args, dict) else {}
            try:
                keys = [_sanitize_token(k, 64) for k in list(safe_args.keys())[:_MAX_ARGS_KEYS]]
            except Exception:
                keys = []
            audit_logger.info(
                "tool_call",
                extra={
                    "event": "tool_call",
                    "user_id": user_id,
                    "session_id": session_id,
                    "tool_name": _sanitize_token(tool_name, _MAX_TOOL_NAME_LEN),
                    "args_hash": _hash_args(safe_args),
                    "args_keys": keys,
                    "success": success,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

    @staticmethod
    def log_tool_failure(user_id: int, tool_name: str, error: str, session_id: int | None = None):
        # 错误原文不进日志：截断+去换行防注入/膨胀，另附 hash 供取证关联。
        try:
            safe_error, error_hash = _safe_error_text(error)
            audit_logger.warning(
                "tool_failure",
                extra={
                    "event": "tool_failure",
                    "user_id": user_id,
                    "session_id": session_id,
                    "tool_name": _sanitize_token(tool_name, _MAX_TOOL_NAME_LEN),
                    "error": safe_error,
                    "error_hash": error_hash,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

    @staticmethod
    def log_rate_limit_hit(user_id: int, endpoint: str):
        try:
            audit_logger.warning(
                "rate_limit_hit",
                extra={
                    "event": "rate_limit_hit",
                    "user_id": user_id,
                    "endpoint": _sanitize_token(endpoint, _MAX_ENDPOINT_LEN),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass


__all__ = ["AuditLogger", "audit_logger"]
