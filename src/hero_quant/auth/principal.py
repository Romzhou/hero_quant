"""auth.principal — 身份主体 Principal 与角色矩阵（viewer < analyst < admin）。

职责：把已验签的 JWT claims 收窄为强类型 Principal，并提供单向角色包含判定。
架构位置：auth 包核心模型；deps/middleware/context/quota 均以此为输入。
关键设计：
- fail-closed：缺 sub / 缺 tenant_id 一律抛错，绝不补默认值；
- 角色只认白名单三个值，未知角色忽略（不授予、不报错），避免 IdP 侧拼写漂移放大权限；
- Keycloak 兼容：roles 缺失时回退读取 realm_access.roles。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from hero_quant.auth.errors import InsufficientRole, InvalidToken, MissingTenant

logger = logging.getLogger(__name__)

# 中文：租户 claim 键名（tenant 为兼容别名；两者并存且不一致时拒绝，防混淆代理）。
_TENANT_CLAIMS = ("tenant_id", "tenant")


class Role(str, Enum):
    """RBAC 角色；等级单向包含（admin ⊃ analyst ⊃ viewer）。"""

    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


_ROLE_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.ANALYST: 2, Role.ADMIN: 3}


def coerce_role(value: Role | str) -> Role:
    """把外部传入的角色名归一为 Role；非法值立即报错（调用方是代码而非用户输入）。"""
    if isinstance(value, Role):
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        try:
            return Role(key)
        except ValueError as e:
            raise ValueError(f"unknown role: {value!r}") from e
    raise TypeError(f"role must be Role or str, got {type(value).__name__}")


def _claim_str(value: Any) -> str:
    """claims 取值：仅接受非空字符串（数字/布尔/对象一律视为缺失）。"""
    if isinstance(value, str):
        return value.strip()
    return ""


def _normalize_roles(raw: Any) -> frozenset[Role]:
    """解析 roles claim：支持列表/元组/集合与空格或逗号分隔字符串；未知值忽略。"""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        items: Iterable[Any] = raw.replace(",", " ").split()
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = raw
    else:
        return frozenset()
    roles: set[Role] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            roles.add(Role(item.strip().lower()))
        except ValueError:
            logger.debug("auth.unknown_role_ignored role=%r", item)
    return frozenset(roles)


@dataclass(frozen=True, slots=True)
class Principal:
    """已认证主体；id/tenant 属性对齐既有 request.state.current_user 读取口径。"""

    subject: str
    tenant_id: str
    roles: frozenset[Role] = frozenset()
    email: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def id(self) -> str:
        """兼容 api.rate_limiter 对 `user.id` 的读取。"""
        return self.subject

    @property
    def tenant(self) -> str:
        """兼容既有代码对 `user.tenant` 的读取。"""
        return self.tenant_id

    def has_role(self, required: Role | str) -> bool:
        """等级包含判定：admin 满足 analyst/viewer，analyst 满足 viewer。"""
        rank = _ROLE_RANK[coerce_role(required)]
        return any(_ROLE_RANK.get(role, 0) >= rank for role in self.roles)

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> "Principal":
        """从验签后的 claims 构造主体；缺 sub/tenant 直接抛错（不猜默认租户）。"""
        if not isinstance(claims, Mapping):
            raise InvalidToken("claims_not_mapping")
        subject = _claim_str(claims.get("sub"))
        if not subject:
            raise InvalidToken("missing_subject")
        tenants = [value for value in (_claim_str(claims.get(key)) for key in _TENANT_CLAIMS) if value]
        if not tenants:
            raise MissingTenant("missing_tenant_claim")
        if len(set(tenants)) > 1:
            raise InvalidToken("conflicting_tenant_claim")
        roles_raw = claims.get("roles")
        if roles_raw is None:
            realm_access = claims.get("realm_access")
            if isinstance(realm_access, Mapping):
                roles_raw = realm_access.get("roles")
        return cls(
            subject=subject,
            tenant_id=tenants[0],
            roles=_normalize_roles(roles_raw),
            email=_claim_str(claims.get("email")) or None,
            claims=claims,
        )


def ensure_role(principal: Principal, required: Role | str) -> Principal:
    """校验角色，不足抛 InsufficientRole（消息只含角色名，不含 token）。"""
    if not principal.has_role(required):
        need = coerce_role(required)
        raise InsufficientRole(f"role '{need.value}' required")
    return principal


__all__ = ["Principal", "Role", "coerce_role", "ensure_role"]
