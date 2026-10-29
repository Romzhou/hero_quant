"""D 线接线单测：auth 中间件/RBAC、tenant→checkpoint、kill switch、gateway 降级、session 作用域。

不连 externals：stub verifier + monkeypatch KillSwitch；只验证接线语义与错误映射。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import hero_quant.api.server as srv
from hero_quant.auth.context import bind_principal
from hero_quant.auth.deps import require_role
from hero_quant.auth.errors import InvalidToken
from hero_quant.auth.principal import Principal


class _StubVerifier:
    """按 Bearer token 内容返回固定 claims；未知 token 按无效处理。"""

    def verify(self, token: str):
        if token in ("analyst", "viewer"):
            return {
                "sub": f"user-{token}",
                "tenant_id": "tenant-a",
                "roles": [token],
            }
        raise InvalidToken("unknown_stub_token")


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.fixture()
def wired_app(monkeypatch):
    saved = srv._AUTH_WIRED
    app = FastAPI()
    assert srv._wire_auth(app, enabled=True, verifier=_StubVerifier()) is True

    @app.get("/analyst")
    async def _analyst(principal: Principal = Depends(require_role("analyst"))):
        return {"sub": principal.subject}

    @app.get("/viewer")
    async def _viewer(principal: Principal = Depends(require_role("viewer"))):
        return {"sub": principal.subject}

    try:
        yield app
    finally:
        srv._AUTH_WIRED = saved


def test_auth_wiring_rejects_without_bearer(wired_app):
    response = TestClient(wired_app).get("/analyst")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_auth_wiring_enforces_role_matrix(wired_app):
    client = TestClient(wired_app)
    assert client.get("/analyst", headers={"Authorization": "Bearer analyst"}).status_code == 200
    forbidden = client.get("/analyst", headers={"Authorization": "Bearer viewer"})
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "insufficient_role"
    assert client.get("/viewer", headers={"Authorization": "Bearer viewer"}).status_code == 200


def test_auth_wiring_disabled_is_noop():
    app = FastAPI()
    assert srv._wire_auth(app, enabled=False) is False


def test_checkpoint_thread_uses_tenant_context():
    principal = Principal.from_claims({"sub": "u1", "tenant_id": "tenant-a"})
    request = _Req({"X-Request-ID": "req-123"})
    with bind_principal(principal):
        thread, tenant = srv._checkpoint_thread_for_request(request)
    assert (thread, tenant) == ("req-123", "tenant-a")


def test_checkpoint_thread_falls_back_to_default_without_auth():
    thread, tenant = srv._checkpoint_thread_for_request(_Req({}))
    assert (thread, tenant) == ("adhoc", "default")


def test_kill_switch_active_maps_to_503(monkeypatch):
    from hero_quant.killswitch.switch import KillSwitch, KillSwitchActive

    async def _active(self, scope, target, *, tenant_id=None):
        raise KillSwitchActive(scope=scope, target=target, reason="maintenance", tenant_id=tenant_id)

    monkeypatch.setattr(KillSwitch, "check", _active)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(srv._enforce_kill_switch("tenant-a", model="gpt-4o-mini"))
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "killswitch_active"
    assert exc.value.detail["kill_reason"] == "maintenance"
    assert exc.value.detail["target"] == "tenant-a"


def test_kill_switch_unavailable_maps_to_503(monkeypatch):
    from hero_quant.killswitch.switch import KillSwitch, KillSwitchUnavailable

    async def _down(self, scope, target, *, tenant_id=None):
        raise KillSwitchUnavailable("redis_unavailable")

    monkeypatch.setattr(KillSwitch, "check", _down)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(srv._enforce_kill_switch("tenant-a"))
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "killswitch_unavailable"


def test_kill_switch_skipped_without_tenant():
    assert asyncio.run(srv._enforce_kill_switch(None, model="gpt-4o-mini")) is None


def test_session_scope_requires_authenticated_tenant():
    with pytest.raises(HTTPException) as exc:
        srv._session_scope()
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "auth_required_for_sessions"


def test_gateway_degraded_reason_only_on_failover():
    state = srv.app.state
    had_health = hasattr(state, "llm_health")
    old = getattr(state, "llm_health", None)
    try:
        if had_health:
            delattr(state, "llm_health")
        assert srv._gateway_degraded_reason() is None
        state.llm_health = {"ok": False, "degraded": False, "degraded_reason": "all_providers_failed:health_check"}
        assert srv._gateway_degraded_reason() is None
        state.llm_health = {"ok": True, "degraded": True, "degraded_reason": "provider_failover:openai->deepseek"}
        assert srv._gateway_degraded_reason() == "llm_gateway:provider_failover:openai->deepseek"
    finally:
        if had_health:
            state.llm_health = old
        elif hasattr(state, "llm_health"):
            delattr(state, "llm_health")
