"""infra.session — Redis Session 存储（hero:session:{id} JSON，TTL 7d）。

职责：多实例共享的用户会话读写；序列化失败/Redis 缺失时 fail-closed（set False/get None）。
架构位置：infra 层，基于 infra.redis.get_redis 接入。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

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
        logger.debug("session.invalid_id op=set")
        return False
    if not isinstance(data, dict):
        # get 只返回 dict，非 dict 写入永远不可读，直接拒绝避免误导性成功
        logger.debug("session.encode_failed error=non-dict data type=%s", type(data).__name__)
        return False
    client = await get_redis()
    if client is None:
        return False
    try:
        # 不用 default=str：不可序列化值必须 loudly 失败，而非静默转字符串
        payload = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug("session.encode_failed error=%s", str(e))
        return False
    try:
        await client.set(_key(norm), payload, ex=SESSION_TTL)
        return True
    except Exception as e:
        logger.debug("session.set_failed error=%s", str(e))
        return False


async def get_session(session_id: str) -> dict[str, Any] | None:
    """读取会话；缺失/过期/损坏返回 None。"""
    from hero_quant.infra.redis import get_redis

    norm = _norm_id(session_id)
    if norm is None:
        logger.debug("session.invalid_id op=get")
        return None
    client = await get_redis()
    if client is None:
        return None
    try:
        raw = await client.get(_key(norm))
    except Exception as e:
        logger.debug("session.get_failed error=%s", str(e))
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            # 严格解码：坏字节抛错走 None，禁止 errors=ignore 吞成错误 dict
            raw = bytes(raw).decode("utf-8")
        obj = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError) as e:
        logger.debug("session.decode_failed error=%s", str(e))
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["SESSION_PREFIX", "SESSION_TTL", "get_session", "set_session"]
