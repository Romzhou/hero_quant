"""api.middleware.security_headers — 安全响应头中间件。

职责：统一附加 nosniff / frame-deny 等最小安全头。
参考 skills/fastapi-agent-module-skill/references/security_headers.py。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

DEFAULT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为每个响应附加默认安全头（setdefault 语义，不覆盖已有值）。"""

    def __init__(self, app, extra_headers: dict | None = None):
        super().__init__(app)
        self.extra_headers = {**DEFAULT_SECURITY_HEADERS, **(extra_headers or {})}

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for header, value in self.extra_headers.items():
            response.headers.setdefault(header, value)
        return response


__all__ = ["SecurityHeadersMiddleware", "DEFAULT_SECURITY_HEADERS"]
