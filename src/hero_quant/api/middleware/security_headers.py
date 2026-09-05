"""api.middleware.security_headers — 安全响应头中间件。

职责：统一附加 nosniff / frame-deny 等最小安全头。
参考 skills/fastapi-agent-module-skill/references/security_headers.py。
"""

from __future__ import annotations

import logging

from starlette.datastructures import MutableHeaders

logger = logging.getLogger(__name__)

DEFAULT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # HSTS：仅在 TLS 后生效；本地 http 开发环境下浏览器忽略，无害。
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    """为每个响应附加默认安全头（setdefault 语义，不覆盖已有值）。

    纯 ASGI 实现：在 `http.response.start` 消息上注入，即使下游返回错误响应
    （4xx/5xx Response）头依然存在。注意：未处理异常一路抛到最外层
    ServerErrorMiddleware 时，500 由其直接发送，本中间件无法触及——该残留风险
    见 server.py，必须靠 app 级 exception handler 或外层反代补头。
    """

    def __init__(self, app, extra_headers: dict | None = None):
        self.app = app
        # 大小写归一合并：HTTP 头名大小写不敏感，未归一会导致重复冲突头。
        merged: dict[str, tuple[str, str]] = {}
        for k, v in list(DEFAULT_SECURITY_HEADERS.items()) + list((extra_headers or {}).items()):
            if not isinstance(k, str) or not isinstance(v, str):
                raise TypeError(f"invalid security header {k!r}: expected (str, str)")
            merged[k.lower()] = (k, v)
        self.extra_headers = {orig_k: v for _, (orig_k, v) in merged.items()}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extra = self.extra_headers

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                try:
                    headers = MutableHeaders(raw=message.setdefault("headers", []))
                    for k, v in extra.items():
                        try:
                            if k.lower() not in headers:
                                headers.append(k, v)
                        except (ValueError, TypeError, UnicodeEncodeError) as e:
                            # 单个头注入失败仅跳过该头并告警（已在响应 start 上，能加的已加），
                            # 不吞整体：继续外层 send，保证响应仍可发出。
                            logger.warning("security-headers: skip header %r: %s", k, e)
                            continue
                except Exception as e:
                    logger.warning("security-headers: injection failed: %s", e)
            await send(message)

        await self.app(scope, receive, send_wrapper)


__all__ = ["SecurityHeadersMiddleware", "DEFAULT_SECURITY_HEADERS"]
