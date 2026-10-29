"""W1/PR-102/103：Principal 解析、角色矩阵与租户上下文。

不依赖 FastAPI/Redis；只验证 fail-closed 语义与 contextvars 隔离。
"""

from __future__ import annotations

import pytest

from hero_quant.auth.context import (
    bind_principal,
    current_tenant_id,
    require_tenant,
    tenant_scoped_id,
)
from hero_quant.auth.errors import InsufficientRole, InvalidToken, MissingTenant
from hero_quant.auth.principal import Principal, Role, ensure_role


def _claims(**overrides):
    claims = {"sub": "user-1", "tenant_id": "tenant-a", "email": "u1@example.com", "roles": ["analyst"]}
    claims.update(overrides)
    return claims


def test_principal_from_claims():
    principal = Principal.from_claims(_claims())
    assert principal.subject == "user-1"
    assert principal.tenant_id == "tenant-a"
    assert principal.email == "u1@example.com"
    # 兼容既有 rate_limiter 的读取口径
    assert principal.id == "user-1"
    assert principal.tenant == "tenant-a"


def test_roles_accept_space_separated_string():
    principal = Principal.from_claims(_claims(roles="viewer admin"))
    assert principal.roles == frozenset({Role.VIEWER, Role.ADMIN})


def test_roles_fall_back_to_keycloak_realm_access():
    claims = _claims()
    claims.pop("roles")
    claims["realm_access"] = {"roles": ["analyst", "offline_access"]}
    principal = Principal.from_claims(claims)
    assert principal.roles == frozenset({Role.ANALYST})


def test_unknown_roles_ignored():
    principal = Principal.from_claims(_claims(roles=["superuser", "admin"]))
    assert principal.roles == frozenset({Role.ADMIN})
    assert principal.has_role(Role.VIEWER)


def test_missing_subject_rejected():
    with pytest.raises(InvalidToken) as exc:
        Principal.from_claims(_claims(sub="   "))
    assert str(exc.value) == "missing_subject"


def test_missing_tenant_rejected():
    claims = _claims()
    claims.pop("tenant_id")
    with pytest.raises(MissingTenant):
        Principal.from_claims(claims)


def test_conflicting_tenant_claims_rejected():
    with pytest.raises(InvalidToken) as exc:
        Principal.from_claims(_claims(tenant="tenant-b"))
    assert str(exc.value) == "conflicting_tenant_claim"


def test_role_hierarchy():
    viewer = Principal.from_claims(_claims(roles=["viewer"]))
    analyst = Principal.from_claims(_claims(roles=["analyst"]))
    admin = Principal.from_claims(_claims(roles=["admin"]))
    assert viewer.has_role(Role.VIEWER)
    assert not viewer.has_role(Role.ANALYST)
    assert analyst.has_role(Role.VIEWER)
    assert not analyst.has_role(Role.ADMIN)
    assert admin.has_role(Role.VIEWER) and admin.has_role(Role.ANALYST) and admin.has_role(Role.ADMIN)
    assert not Principal.from_claims(_claims(roles=[])).has_role(Role.VIEWER)


def test_ensure_role_raises_insufficient_role():
    viewer = Principal.from_claims(_claims(roles=["viewer"]))
    assert ensure_role(viewer, "viewer") is viewer
    with pytest.raises(InsufficientRole):
        ensure_role(viewer, "analyst")


def test_require_tenant_without_context_fails_closed():
    with pytest.raises(MissingTenant):
        require_tenant()
    assert current_tenant_id() is None


def test_bind_principal_sets_and_restores_context():
    principal = Principal.from_claims(_claims())
    with bind_principal(principal):
        assert require_tenant() == "tenant-a"
    assert current_tenant_id() is None


def test_nested_principal_scopes_restore_previous():
    first = Principal.from_claims(_claims(sub="u1", tenant_id="tenant-a"))
    second = Principal.from_claims(_claims(sub="u2", tenant_id="tenant-b"))
    with bind_principal(first):
        with bind_principal(second):
            assert require_tenant() == "tenant-b"
        assert require_tenant() == "tenant-a"


def test_tenant_scoped_id_uses_context_and_validates():
    principal = Principal.from_claims(_claims())
    with bind_principal(principal):
        assert tenant_scoped_id("thread-1") == "tenant-a:thread-1"
    assert tenant_scoped_id("thread-2", tenant_id="tenant-c") == "tenant-c:thread-2"
    with pytest.raises(ValueError):
        tenant_scoped_id("thread-3", tenant_id="bad:tenant")
    with pytest.raises(ValueError):
        tenant_scoped_id("  ", tenant_id="tenant-c")
