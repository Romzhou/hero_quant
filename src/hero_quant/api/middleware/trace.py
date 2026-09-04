"""api.middleware.trace — TraceId 中间件与 contextvars 透传。

职责：透传 x-request-id（兼 x-trace-id），注入响应头并绑定 contextvars。
关键设计：参考 skills/fastapi-agent-module-skill/references/trace.py，
但统一以 x-request-id 为主（server.py 已有约定 trace_id=request_id）。
"""

from __future__ import annotations

import contextvars
import uuid
from typing import Optional

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_trace_id(trace_id: Optional[str] = None) -> str:
    """设置当前上下文 trace_id，None 则自动生成 16 位 hex。"""
    if trace_id is None:
        trace_id = uuid.uuid4().hex[:16]
    _trace_id_var.set(trace_id)
    return trace_id


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
            rid = request.headers.get("x-request-id") or request.headers.get("x-trace-id")
            if not rid:
                rid = uuid.uuid4().hex[:16]
            set_trace_id(rid)
            _request_id_var.set(rid)
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            response.headers["X-Trace-Id"] = rid
            return response

except ImportError:  # pragma: no cover - 无 starlette 时跳过
    TraceIdMiddleware = None  # type: ignore[assignment]


__all__ = [
    "TraceIdMiddleware",
    "set_trace_id",
    "get_trace_id",
    "get_request_id",
    "clear_trace_id",
]
