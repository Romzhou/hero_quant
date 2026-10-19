"""infra.session — Redis Session 存储（hero:session:{id} JSON，TTL 7d）。

职责：多实例共享的用户会话读写；序列化失败/Redis 缺失时 fail-closed（set False/get None）。
架构位置：infra 层，基于 infra.redis.get_redis 接入。

中文：G-13——fixation 前置：session_id 必须由服务端 generate_session_id() 生成（密码学随机，
不可接受客户端供给的 id 直接建会话）；启用身份绑定时用 bind_session_owner() 显式绑定，
消费端用 check_session_owner() 校验归属。既有 set/get 保持原语义（归一化 + 空 id 拒绝）。
"""

from __future__ import annotations

import json
import logging
import secrets
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
# 服务端生成 id 熵：32 字节 urlsafe（~43 字符，不可预测，防 fixation/枚举）
SERVER_SESSION_ID_BYTES = 32


def generate_session_id() -> str:
    """服务端生成 session_id（G-13 fixation 前置：调用方不得用客户端供给 id 建会话）。

    中文：密码学随机 token_urlsafe，归一化必过（非空、远小于 128 上限）；失败永不回退
    可预测值（secrets 抛错即向上传播，由调用方 fail-closed，禁止收敛共享键）。
    """
    return secrets.token_urlsafe(SERVER_SESSION_ID_BYTES)


async def create_session(data: dict[str, Any], owner: str | None = None) -> str | None:
    """服务端建会话：生成随机 id 并写入，返回 id；失败返回 None（fail-closed）。

    中文：G-13——新建会话唯一入口（防 fixation）；owner 非空时一并写入归属绑定
    （_owner 保留键），消费端用 check_session_owner() 校验。data 非 dict 直接拒绝。
    """
    sid = generate_session_id()
    payload = dict(data) if isinstance(data, dict) else None
    if payload is None:
        _warn("session.encode_failed", "session.encode_failed error=non-dict data type=%s error_code=session.encode_failed", type(data).__name__)
        return None
    if isinstance(owner, str) and owner.strip():
        payload = dict(payload)
        payload["_owner"] = owner.strip()
    ok = await set_session(sid, payload)
    return sid if ok else None


async def check_session_owner(session_id: str, owner: str) -> bool:
    """校验会话归属：存储的 _owner 与给定 owner 一致才 True；缺失/损坏/不一致一律 False。"""
    if not isinstance(owner, str) or not owner.strip():
        return False
    doc = await get_session(session_id)
    if not isinstance(doc, dict):
        return False
    try:
        bound = doc.get("_owner")
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(bound, str) or not bound:
        return False
    try:
        import hmac as _hmac

        return _hmac.compare_digest(bound.strip(), owner.strip())
    except (TypeError, ValueError, AttributeError):
        return False


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


__all__ = [
    "SESSION_PREFIX",
    "SESSION_TTL",
    "check_session_owner",
    "create_session",
    "generate_session_id",
    "get_session",
    "set_session",
]
