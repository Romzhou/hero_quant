"""api.rate_limiter — slowapi 三档限流胶水（chat 10/min，session 30/min，tool 60/min）。

职责：FastAPI Depends 依赖；限流 key=user:{id} or ip；配额单实现复用 infra RateLimiter，
slowapi 仅作可选 Depends/装饰层（未安装时降级为 infra 直调，不影响配额语义）。
架构位置：api 网关层，位于具体路由之前。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from hero_quant.infra.redis import RateLimiter  # 顶层导入，避免每请求函数内导入

logger = logging.getLogger(__name__)

CHAT_MAX = 10
SESSION_MAX = 30
TOOL_MAX = 60
WINDOW_SECONDS = 60

try:
    from slowapi import Limiter as _SlowLimiter  # type: ignore

    SLOWAPI_AVAILABLE = True
except ImportError:
    _SlowLimiter = None  # type: ignore
    SLOWAPI_AVAILABLE = False


def limit_key(request: Request) -> str:
    """限流 key：优先已认证 user_id，否则回退客户端 IP。"""
    user = getattr(getattr(request, "state", None), "current_user", None)
    uid = getattr(user, "id", None) if user is not None else None
    # 显式 is not None 判定：0 为合法 id，不得当匿名
    if uid is not None and uid != "":
        return f"user:{uid}"
    try:
        ip = getattr(getattr(request, "client", None), "host", None) or "unknown"
    except Exception:
        ip = "unknown"
    return f"ip:{ip}"


# slowapi 实例仅用于 app.state 接线（生产 gate）；配额判定走 infra RateLimiter 单实现。
limiter = _SlowLimiter(key_func=limit_key) if SLOWAPI_AVAILABLE and _SlowLimiter is not None else None


async def _check(request: Request, quota: int, endpoint: str) -> bool:
    """按 endpoint 隔离 bucket；Redis 故障 fail-closed 抛 503。"""
    try:
        # 三档隔离：key 包含 endpoint 前缀，避免 chat/tool/session 共用同一桶
        ok = await RateLimiter().try_acquire(f"{endpoint}:{limit_key(request)}", quota, WINDOW_SECONDS)
    except Exception as e:
        # fail-closed：限流后端故障时不放行，避免在最需限流时失守
        logger.warning("ratelimiter.check_failed endpoint=%s error=%s", endpoint, str(e))
        raise HTTPException(status_code=503, detail="Rate limiter unavailable") from e
    if not ok:
        raise HTTPException(status_code=429, detail=f"Too many {endpoint} requests")
    return True


async def rate_limit_chat(request: Request) -> bool:
    """chat 档：每 key 10 次/分钟（Depends 用）。"""
    return await _check(request, CHAT_MAX, "chat")


async def rate_limit_session(request: Request) -> bool:
    """session 档：每 key 30 次/分钟（Depends 用）。"""
    return await _check(request, SESSION_MAX, "session")


async def rate_limit_tool(request: Request) -> bool:
    """tool 档：每 key 60 次/分钟（Depends 用）。"""
    return await _check(request, TOOL_MAX, "tool")


__all__ = [
    "CHAT_MAX",
    "SESSION_MAX",
    "TOOL_MAX",
    "WINDOW_SECONDS",
    "SLOWAPI_AVAILABLE",
    "limit_key",
    "limiter",
    "rate_limit_chat",
    "rate_limit_session",
    "rate_limit_tool",
]
