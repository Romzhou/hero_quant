"""auth.deps — FastAPI 身份依赖（Bearer 解析、Principal 注入、RBAC、错误映射）。

职责：把 auth 包的验签/主体/RBAC 能力接成 FastAPI Depends，并统一 AuthError -> HTTP 映射。
架构位置：auth 包与 api 层的唯一接缝；server.py 挂载时只需 install_auth_error_handlers + Depends。
关键设计：
- verifier 优先取 app.state.auth_verifier（测试/多租户可注入），否则按环境变量懒加载单例；
- 认证成功即写 request.state.principal/current_user/current_tenant，兼容既有 rate_limiter 读取口径；
- 401/403/429/503 映射集中在一处，避免各路由自行拼状态码导致口径漂移。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Awaitable, Callable

from fastapi import Depends, Request, status
from fastapi.responses import JSONResponse

from hero_quant.auth.context import set_principal
from hero_quant.auth.errors import (
    AuthConfigError,
    AuthError,
    InsufficientRole,
    InvalidToken,
    QuotaExceeded,
)
from hero_quant.auth.jwt import JWTVerifier
from hero_quant.auth.oidc import OIDCSettings, build_verifier
from hero_quant.auth.principal import Principal, Role, coerce_role

logger = logging.getLogger(__name__)

_default_lock = threading.Lock()
_default_verifier: JWTVerifier | None = None


def token_from_headers(headers: Any) -> str:
    """从 Authorization 头取 Bearer token；缺失/多值/非 Bearer 一律 InvalidToken。"""
    raw = headers.get("authorization") if hasattr(headers, "get") else None
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidToken("missing_authorization_header")
    parts = raw.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise InvalidToken("malformed_authorization_header")
    token = parts[1].strip()
    if not token:
        raise InvalidToken("missing_bearer_token")
    return token


def extract_bearer_token(request: Request) -> str:
    """Request 版 Bearer 解析。"""
    return token_from_headers(request.headers)


def default_verifier() -> JWTVerifier:
    """按环境变量构建进程级验签器（首次调用时构建；配置缺失抛 AuthConfigError）。"""
    global _default_verifier
    if _default_verifier is not None:
        return _default_verifier
    with _default_lock:
        if _default_verifier is None:
            _default_verifier = build_verifier(OIDCSettings.from_env())
        return _default_verifier


def reset_default_verifier() -> None:
    """清空进程级验签器（测试/配置热更新用）。"""
    global _default_verifier
    with _default_lock:
        _default_verifier = None


def resolve_verifier(request: Request) -> JWTVerifier:
    """优先 app.state 注入，其次环境变量单例。"""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    verifier = getattr(state, "auth_verifier", None) if state is not None else None
    if verifier is not None:
        return verifier
    return default_verifier()


def _bind_request_state(request: Request, principal: Principal) -> None:
    """认证结果落到 request.state（含既有 rate_limiter 依赖的字段名）。"""
    request.state.principal = principal
    request.state.current_user = principal
    request.state.user = principal
    request.state.current_tenant = principal.tenant_id
    request.state.tenant = principal.tenant_id


async def get_current_principal(request: Request) -> Principal:
    """FastAPI 依赖：强制 Bearer 认证并返回 Principal（未认证 401）。"""
    existing = getattr(request.state, "principal", None)
    if isinstance(existing, Principal):
        return existing
    claims = resolve_verifier(request).verify(extract_bearer_token(request))
    principal = Principal.from_claims(claims)
    _bind_request_state(request, principal)
    set_principal(principal)
    return principal


async def get_tenant_id(principal: Principal = Depends(get_current_principal)) -> str:
    """FastAPI 依赖：仅取租户 id（缺失 tenant 在 Principal 构造阶段已 401）。"""
    return principal.tenant_id


def require_role(required: Role | str, *extra: Role | str) -> Callable[..., Awaitable[Principal]]:
    """构造角色依赖：满足任意一个角色即放行（等级包含，admin 通吃）。"""
    wanted = tuple(coerce_role(role) for role in (required, *extra))

    async def _dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not any(principal.has_role(role) for role in wanted):
            names = ",".join(role.value for role in wanted)
            raise InsufficientRole(f"one of roles required: {names}")
        return principal

    _dependency.__name__ = "require_role_" + "_".join(role.value for role in wanted)
    return _dependency


def auth_error_response(exc: AuthError) -> JSONResponse:
    """AuthError -> JSONResponse（401/403/429/503 + code，消息不含敏感材料）。"""
    headers: dict[str, str] = {}
    if isinstance(exc, AuthConfigError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, QuotaExceeded):
        status_code = status.HTTP_429_TOO_MANY_REQUESTS
        headers["Retry-After"] = str(exc.retry_after)
    elif isinstance(exc, InsufficientRole):
        status_code = status.HTTP_403_FORBIDDEN
    else:
        status_code = status.HTTP_401_UNAUTHORIZED
        headers["WWW-Authenticate"] = 'Bearer realm="hero-quant", error="invalid_token"'
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(exc) or exc.code, "code": exc.code},
        headers=headers,
    )


def install_auth_error_handlers(app: Any) -> None:
    """注册 AuthError 处理器（Starlette 按 MRO 命中子类，一处注册覆盖全部）。"""

    async def _handle(_request: Request, exc: AuthError) -> JSONResponse:
        return auth_error_response(exc)

    app.add_exception_handler(AuthError, _handle)


__all__ = [
    "auth_error_response",
    "default_verifier",
    "extract_bearer_token",
    "get_current_principal",
    "get_tenant_id",
    "install_auth_error_handlers",
    "require_role",
    "reset_default_verifier",
    "resolve_verifier",
    "token_from_headers",
]
