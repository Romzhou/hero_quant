"""auth — 身份与租户底座（OIDC 验签、Principal/RBAC、租户上下文、配额）。

职责：为 API 层提供可注入的 Bearer 认证、租户硬边界与租户级配额。
架构位置：src/hero_quant/auth/；仅依赖 stdlib + fastapi/starlette(+可选 cryptography)，
不反向依赖 api/server，便于在 server 串行窗口一次性接线。
关键设计：
- fail-closed：缺 token/缺 tenant/缺角色/配额耗尽/配置缺失都有明确错误码；
- 依赖可注入：app.state.auth_verifier、JWKS fetcher、clock、Redis 均可替身，便于本机 mock 测试；
- 角色单向包含：viewer < analyst < admin，未知角色不授予任何权限。
"""

from __future__ import annotations

from hero_quant.auth.context import (
    bind_principal,
    current_principal,
    current_tenant_id,
    require_principal,
    require_tenant,
    tenant_scoped_id,
)
from hero_quant.auth.deps import (
    auth_error_response,
    default_verifier,
    get_current_principal,
    get_tenant_id,
    install_auth_error_handlers,
    require_role,
    reset_default_verifier,
)
from hero_quant.auth.errors import (
    AuthConfigError,
    AuthError,
    InsufficientRole,
    InvalidToken,
    MissingTenant,
    QuotaExceeded,
)
from hero_quant.auth.jwks import JWKSCache, parse_jwks
from hero_quant.auth.jwt import JWTConfig, JWTVerifier
from hero_quant.auth.middleware import AuthContextMiddleware
from hero_quant.auth.oidc import AuthMode, OIDCSettings, auth_enabled, build_verifier, get_auth_mode
from hero_quant.auth.principal import Principal, Role, ensure_role
from hero_quant.auth.quota import (
    QuotaPolicy,
    TenantQuotaLimiter,
    default_quota,
    reset_default_quota,
    tenant_rate_limit,
)

__all__ = [
    "AuthConfigError",
    "AuthContextMiddleware",
    "AuthError",
    "AuthMode",
    "InsufficientRole",
    "InvalidToken",
    "JWKSCache",
    "JWTConfig",
    "JWTVerifier",
    "MissingTenant",
    "OIDCSettings",
    "Principal",
    "QuotaExceeded",
    "QuotaPolicy",
    "Role",
    "TenantQuotaLimiter",
    "auth_error_response",
    "auth_enabled",
    "bind_principal",
    "build_verifier",
    "current_principal",
    "current_tenant_id",
    "default_quota",
    "default_verifier",
    "ensure_role",
    "get_auth_mode",
    "get_current_principal",
    "get_tenant_id",
    "install_auth_error_handlers",
    "parse_jwks",
    "require_principal",
    "require_role",
    "require_tenant",
    "reset_default_quota",
    "reset_default_verifier",
    "tenant_rate_limit",
    "tenant_scoped_id",
]
