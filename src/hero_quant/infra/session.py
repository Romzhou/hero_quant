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


def _key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}"


async def set_session(session_id: str, data: dict[str, Any]) -> bool:
    """写入会话 JSON，TTL 7 天；成功 True，失败 False。"""
    from hero_quant.infra.redis import get_redis

    client = await get_redis()
    if client is None:
        return False
    try:
        payload = json.dumps(data, ensure_ascii=False, default=str)
    except Exception as e:
        logger.debug("session.encode_failed error=%s", str(e))
        return False
    try:
        await client.set(_key(session_id), payload, ex=SESSION_TTL)
        return True
    except Exception as e:
        logger.debug("session.set_failed error=%s", str(e))
        return False


async def get_session(session_id: str) -> dict[str, Any] | None:
    """读取会话；缺失/过期/损坏返回 None。"""
    from hero_quant.infra.redis import get_redis

    client = await get_redis()
    if client is None:
        return None
    try:
        raw = await client.get(_key(session_id))
    except Exception as e:
        logger.debug("session.get_failed error=%s", str(e))
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="ignore")
        obj = json.loads(raw)
    except Exception as e:
        logger.debug("session.decode_failed error=%s", str(e))
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["SESSION_PREFIX", "SESSION_TTL", "get_session", "set_session"]
