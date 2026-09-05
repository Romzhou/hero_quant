"""api.middleware.trace — TraceId 中间件与 contextvars 透传。

职责：透传 x-request-id（兼 x-trace-id），注入响应头并绑定 contextvars。
关键设计：参考 skills/fastapi-agent-module-skill/references/trace.py，
但统一以 x-request-id 为主（server.py 已有约定 trace_id=request_id）。
"""

from __future__ import annotations

import contextvars
import re
import uuid
from typing import Optional

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# 请求头白名单：兼容 pr3i-trace-123 这类合法 ID，拒绝空白/CRLF/非法字符/超长输入
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9\-_.:]{1,128}\Z")
_MAX_TRACE_ID_LEN = 128


def _clean_trace_id(raw: object) -> str | None:
    """校验外部传入的 trace/request id；非法返回 None，走生成回退。"""
    if not isinstance(raw, str):
        return None
    rid = raw.strip()
    if not rid or len(rid) > _MAX_TRACE_ID_LEN:
        return None
    if _TRACE_ID_RE.fullmatch(rid) is None:
        return None
    return rid


def set_trace_id(trace_id: Optional[str] = None) -> str:
    """设置当前上下文 trace_id，None/空则自动生成 16 位 hex（同步 request_id）。"""
    cleaned = _clean_trace_id(trace_id) if trace_id is not None else None
    if not cleaned:
        cleaned = uuid.uuid4().hex[:16]
    _trace_id_var.set(cleaned)
    _request_id_var.set(cleaned)
    return cleaned


def get_trace_id() -> str:
    """获取当前上下文 trace_id。"""
    return _trace_id_var.get()


def get_request_id() -> str:
    """获取当前上下文 request_id。"""
    return _request_id_var.get()


def clear_trace_id() -> None:
    """清除 trace/request 上下文（测试用）。"""
    _trace_id_var.set("-")
    _request_id_var.set("-")


try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class TraceIdMiddleware(BaseHTTPMiddleware):
        """透传 x-request-id（兼容 x-trace-id），缺失则生成 uuid4。

        请求头优先级：x-request-id > x-trace-id > 自动生成。
        响应头同时回写 x-request-id 与 x-trace-id，便于客户端关联。
        """

        async def dispatch(self, request: Request, call_next):
            # 各自独立校验：非法 x-request-id 不得吞掉合法 x-trace-id 回退。
            rid = (
                _clean_trace_id(request.headers.get("x-request-id"))
                or _clean_trace_id(request.headers.get("x-trace-id"))
                or uuid.uuid4().hex[:16]
            )
            t1 = _trace_id_var.set(rid)
            t2 = _request_id_var.set(rid)
            # BaseHTTPMiddleware 在部分 Starlette 版本下行用拷贝/分离 context 运行，
            # 此处 set() 后 endpoint 侧经同 context 调 call_next 可见；后台任务如需
            # ID 须由调用方显式透传 rid（见 server.py query/query_stream）。
            try:
                response: Response = await call_next(request)
                response.headers["X-Request-ID"] = rid
                response.headers["X-Trace-Id"] = rid
                return response
            finally:
                # token 复位：异常也不泄漏到复用 task/测试/后台任务。
                # 注意：Starlette 在响应 BackgroundTasks 执行前即跑 finally，
                # 后台任务如需 ID 须由调用方显式透传 rid（见 server.py query/query_stream）。
                _trace_id_var.reset(t1)
                _request_id_var.reset(t2)

except ImportError:  # pragma: no cover - 无 starlette 时跳过
    class TraceIdMiddleware:  # type: ignore[no-redef]
        """Fail-fast stub：无 starlette 时实例化即抛，避免 add_middleware(None) 迷惑报错."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("TraceIdMiddleware requires starlette")


def _pure_asgi_trace_middleware_note() -> str:
    """长期方向：纯 ASGI 中间件替代 BaseHTTPMiddleware（request 级 ContextVar 更稳）。

    保持 BaseHTTPMiddleware 兼容（现有单测/挂载均依赖 TraceIdMiddleware 类）；
    后台任务的 ID 透传须由调用方显式传递 rid（server.py query/query_stream），
    不依赖 middleware 自动透传（BaseHTTPMiddleware finally 先于 BackgroundTasks 执行）。
    """
    return "prefer pure-ASGI middleware for request-scoped ContextVars; propagate rid explicitly to background tasks"


__all__ = [
    "TraceIdMiddleware",
    "set_trace_id",
    "get_trace_id",
    "get_request_id",
    "clear_trace_id",
]
