"""auth.middleware — 纯 ASGI 身份中间件（Bearer 校验 + 上下文绑定 + 公共路径放行）。

职责：在进入路由前完成 token 验签与 Principal/tenant 绑定，enforce 模式下失败即 401/503。
架构位置：auth 包；由 api/server.py 在串行窗口挂载（当前不接线）。
关键设计：
- 纯 ASGI 实现，不依赖 Request 生命周期，SSE/流式响应不受影响；
- 健康检查、指标、OpenAPI 文档默认公开（可覆盖），其余一律强制；
- enforce 模式下 AuthConfigError 返回 503，避免把服务端配置故障伪装成 401 诱导重登；
- OFF 模式完全透传（迁移期 feature flag），不做任何身份推断。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from starlette.datastructures import Headers

from hero_quant.auth.context import reset_principal, set_principal
from hero_quant.auth.deps import auth_error_response, token_from_headers
from hero_quant.auth.errors import AuthError
from hero_quant.auth.oidc import AuthMode
from hero_quant.auth.principal import Principal

logger = logging.getLogger(__name__)

DEFAULT_PUBLIC_PATHS: tuple[str, ...] = (
    "/live",
    "/health",
    "/healthz",
    "/ready",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
    # 中文：SPA 壳与静态资源必须匿名可取，否则登录页本身都加载不出来；
    # 数据仍由 /v1/* 的 Bearer 校验保护。
    "/",
    "/index.html",
    "/assets",
    "/static",
    "/favicon.ico",
    "/vite.svg",
)


def _is_public(path: str, public_paths: Iterable[str]) -> bool:
    """精确匹配或子路径匹配（`/health` 覆盖 `/health/live`，不覆盖 `/healthz-extra`）。

    中文：前缀 "/" 只做精确匹配，否则等价于全站放行。
    """
    for prefix in public_paths:
        if path == prefix:
            return True
        if prefix != "/" and path.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


class AuthContextMiddleware:
    """ASGI 中间件：验签 -> 绑定 scope["state"]/contextvars -> 放行或短路。"""

    def __init__(
        self,
        app: Any,
        *,
        verifier: Any = None,
        mode: AuthMode = AuthMode.ENFORCE,
        public_paths: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self._verifier = verifier
        self._mode = mode
        self._public_paths = tuple(public_paths) if public_paths is not None else DEFAULT_PUBLIC_PATHS

    def _resolve_verifier(self, scope: dict[str, Any]) -> Any:
        if self._verifier is not None:
            return self._verifier
        app_state = scope.get("app")
        state = getattr(app_state, "state", None)
        verifier = getattr(state, "auth_verifier", None) if state is not None else None
        if verifier is not None:
            return verifier
        from hero_quant.auth.deps import default_verifier

        return default_verifier()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if self._mode is AuthMode.OFF or _is_public(path, self._public_paths):
            await self.app(scope, receive, send)
            return
        try:
            token = token_from_headers(Headers(scope=scope))
            claims = self._resolve_verifier(scope).verify(token)
            principal = Principal.from_claims(claims)
        except AuthError as exc:
            logger.info("auth.request_rejected path=%s code=%s", path, exc.code)
            await auth_error_response(exc)(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state.update(
            {
                "principal": principal,
                "current_user": principal,
                "user": principal,
                "current_tenant": principal.tenant_id,
                "tenant": principal.tenant_id,
            }
        )
        reset_token = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(reset_token)


__all__ = ["AuthContextMiddleware", "DEFAULT_PUBLIC_PATHS"]
