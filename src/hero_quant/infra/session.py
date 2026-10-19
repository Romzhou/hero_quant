"""infra.session — Redis Session 存储（hero:session:{id} JSON，TTL 7d）。

职责：多实例共享的用户会话读写；序列化失败/Redis 缺失时 fail-closed（set False/get None）。
架构位置：infra 层，基于 infra.redis.get_redis 接入。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    import structlog as _structlog  # type: ignore

    _slog = _structlog.get_logger(__name__)
except Exception:  # pragma: no cover — 最小环境无 structlog
    _slog = None  # type: ignore

_CHAIN = "A"  # F线分级：本文件位于问答链（会话读写，会话丢失即上下文丢失）


def _warn(op: str, msg: str, *args: Any, **fields: Any) -> None:
    """F线分级告警（D线 risk/_log_silent_handled 同款 schema）：stdlib warning 为主
    （caplog/现网采集兼容），structlog 结构化为辅（error_code + chain；trace_id 由
    server.py contextvars 自动透传）。PII 约束：fields 只传非敏感字段，禁 session 原值。"""
    _exc_info = fields.pop("exc_info", None)
    try:
        logger.warning(msg, *args, exc_info=_exc_info)
    except Exception:
        pass
    if _slog is None:
        return
    try:
        _slog.warning(op, error_code=op, chain=fields.pop("chain", _CHAIN), exc_info=_exc_info, **fields)
    except Exception:
        pass

SESSION_PREFIX = "hero:session:"
SESSION_TTL = 7 * 24 * 3600
# session_id 上限：防超大键 DoS；归一化后仍超长直接拒绝
MAX_SESSION_ID_LEN = 128


def _norm_id(session_id: str) -> str | None:
    """归一化 id：非 str/空白/超长返回 None；首尾空白折叠到同一键。"""
    if not isinstance(session_id, str):
        return None
    sid = session_id.strip()
    if not sid or len(sid) > MAX_SESSION_ID_LEN:
        return None
    return sid


def _key(session_id: str) -> str:
    """拼接 Redis 键；空 id 直接抛错，禁止收敛到共享键（防跨会话污染）。"""
    sid = _norm_id(session_id)
    if sid is None:
        raise ValueError("session_id must be a non-empty str")
    return f"{SESSION_PREFIX}{sid}"


async def set_session(session_id: str, data: dict[str, Any]) -> bool:
    """写入会话 JSON，TTL 7 天；成功 True，失败 False。"""
    from hero_quant.infra.redis import get_redis

    norm = _norm_id(session_id)
    if norm is None:
        _warn("session.invalid_id", "session.invalid_id op=set error_code=session.invalid_id")
        return False
    if not isinstance(data, dict):
        # get 只返回 dict，非 dict 写入永远不可读，直接拒绝避免误导性成功
        _warn("session.encode_failed", "session.encode_failed error=non-dict data type=%s error_code=session.encode_failed", type(data).__name__)
        return False
    client = await get_redis()
    if client is None:
        return False
    try:
        # 不用 default=str：不可序列化值必须 loudly 失败，而非静默转字符串
        payload = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        _warn("session.encode_failed", "session.encode_failed error=%s error_code=session.encode_failed", str(e))
        return False
    try:
        await client.set(_key(norm), payload, ex=SESSION_TTL)
        return True
    except Exception as e:
        _warn("session.set_failed", "session.set_failed error=%s error_code=session.set_failed", str(e))
        return False


async def get_session(session_id: str) -> dict[str, Any] | None:
    """读取会话；缺失/过期/损坏返回 None。"""
    from hero_quant.infra.redis import get_redis

    norm = _norm_id(session_id)
    if norm is None:
        _warn("session.invalid_id", "session.invalid_id op=get error_code=session.invalid_id")
        return None
    client = await get_redis()
    if client is None:
        return None
    try:
        raw = await client.get(_key(norm))
    except Exception as e:
        _warn("session.get_failed", "session.get_failed error=%s error_code=session.get_failed", str(e))
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            # 严格解码：坏字节抛错走 None，禁止 errors=ignore 吞成错误 dict
            raw = bytes(raw).decode("utf-8")
        obj = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError) as e:
        _warn("session.decode_failed", "session.decode_failed error=%s error_code=session.decode_failed", str(e))
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["SESSION_PREFIX", "SESSION_TTL", "get_session", "set_session"]
