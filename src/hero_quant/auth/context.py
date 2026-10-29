"""auth.context — 请求级身份/租户上下文（contextvars）与租户作用域键。

职责：把已认证的 Principal 绑定到当前任务上下文，供数据访问层强制取 tenant。
架构位置：auth 包；deps/middleware 写入，checkpoint/billing/memory/session 读取。
关键设计：
- 只读入口 require_tenant() 在未绑定租户时 fail-closed 抛 MissingTenant（绝不回退默认租户）；
- tenant_scoped_id() 统一「tenant:业务键」前缀，拒绝含冒号的租户名以保证解析无歧义；
- contextvars 天然按任务隔离，并发请求之间不会串租户。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from hero_quant.auth.errors import InvalidToken, MissingTenant
from hero_quant.auth.principal import Principal

logger = logging.getLogger(__name__)

_principal_var: ContextVar[Principal | None] = ContextVar("hero_auth_principal", default=None)


def current_principal() -> Principal | None:
    """当前任务已认证主体；未认证返回 None（调用方决定是否拒绝）。"""
    return _principal_var.get()


def require_principal() -> Principal:
    """取当前主体；未认证抛 InvalidToken（映射 401）。"""
    principal = _principal_var.get()
    if principal is None:
        raise InvalidToken("authentication_required")
    return principal


def current_tenant_id() -> str | None:
    """当前任务租户；未绑定返回 None。"""
    principal = _principal_var.get()
    return principal.tenant_id if principal is not None else None


def require_tenant() -> str:
    """取当前租户；缺失即 fail-closed（数据访问入口必须调用）。"""
    principal = _principal_var.get()
    if principal is None or not principal.tenant_id:
        raise MissingTenant("tenant context is not bound")
    return principal.tenant_id


def set_principal(principal: Principal) -> Token[Principal | None]:
    """绑定主体并返回复位 token（供 finally 恢复，避免上下文泄漏）。"""
    return _principal_var.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    """按 token 复位；token 跨上下文失效时只告警，不掩盖主流程结果。"""
    try:
        _principal_var.reset(token)
    except (ValueError, RuntimeError) as e:  # pragma: no cover — 仅在错误跨上下文复位时触发
        logger.warning("auth.context_reset_failed error=%s", e)


@contextmanager
def bind_principal(principal: Principal) -> Iterator[Principal]:
    """临时绑定主体（测试/后台任务）；退出时恢复原值。"""
    token = set_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)


def tenant_scoped_id(business_id: str, *, tenant_id: str | None = None) -> str:
    """生成租户作用域键 `{tenant}:{business_id}`（checkpoint thread / memory 命名空间等）。"""
    tenant = require_tenant() if tenant_id is None else tenant_id
    if not isinstance(tenant, str) or not tenant.strip():
        raise MissingTenant("tenant context is not bound")
    tenant = tenant.strip()
    if ":" in tenant:
        raise ValueError("tenant_id must not contain ':'")
    if not isinstance(business_id, str) or not business_id.strip():
        raise ValueError("business_id must be a non-empty string")
    return f"{tenant}:{business_id.strip()}"


__all__ = [
    "bind_principal",
    "current_principal",
    "current_tenant_id",
    "require_principal",
    "require_tenant",
    "reset_principal",
    "set_principal",
    "tenant_scoped_id",
]
