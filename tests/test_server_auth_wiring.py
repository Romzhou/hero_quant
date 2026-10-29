"""D 线接线的**端点层/挂载层**守卫（与 tests/test_server_dline.py 互补，不重复）。

分工：`test_server_dline.py` 锁 helper 语义（401/角色矩阵、checkpoint tenant、kill switch
转 503、gateway 降级口径）；本文件锁两件它没有覆盖的事——

1. `_wire_auth` 的**挂载判据与 fail-closed**（未启用不挂、配不齐必须启动失败、注入后中间件入栈）；
2. `/v1/sessions*` **HTTP 端点**的租户边界、错误映射（404/503/400）与续跑游标。

不触碰包内逻辑，不启动真 PG/Redis（store 一律注入替身）。规模：S。
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hero_quant.api import server as srv
from hero_quant.auth.errors import AuthConfigError
from hero_quant.auth.principal import Principal, Role


def _principal(tenant: str = "tenant-a", subject: str = "user-1", *roles: Role) -> Principal:
    return Principal(
        subject=subject,
        tenant_id=tenant,
        roles=frozenset(roles) if roles else frozenset({Role.VIEWER}),
    )


# ---------------------------------------------------------------------------
# _wire_auth：挂载判据与 fail-closed
# ---------------------------------------------------------------------------


def test_wire_auth_off_mode_never_mounts_middleware():
    """auth 未启用时：不挂中间件、不改 app.state，返回 False（保留匿名链路）。"""
    from hero_quant.auth.middleware import AuthContextMiddleware

    app = FastAPI()
    assert srv._wire_auth(app, enabled=False) is False
    assert getattr(app.state, "auth_verifier", None) is None
    assert not any(m.cls is AuthContextMiddleware for m in app.user_middleware)


def test_wire_auth_fail_closed_when_oidc_incomplete():
    """要求强制认证但 OIDC 配不齐：必须抛错（启动失败），绝不静默退回匿名。"""
    app = FastAPI()
    with pytest.raises(AuthConfigError):
        srv._wire_auth(app, enabled=True)


def test_wire_auth_mounts_middleware_and_binds_verifier():
    """注入验签器后：state.auth_verifier 可用 + 中间件入栈 + 错误处理器注册。"""
    from hero_quant.auth import AuthError
    from hero_quant.auth.middleware import AuthContextMiddleware

    class _StubVerifier:
        def verify(self, token: str) -> dict:
            return {"sub": "user-1", "tenant_id": "tenant-a", "roles": ["viewer"]}

    app = FastAPI()
    wired_before = srv._AUTH_WIRED
    try:
        assert srv._wire_auth(app, enabled=True, verifier=_StubVerifier()) is True
    finally:
        srv._AUTH_WIRED = wired_before
    assert app.state.auth_verifier is not None
    assert any(m.cls is AuthContextMiddleware for m in app.user_middleware)
    assert AuthError in app.exception_handlers


def test_request_tenant_none_without_principal():
    """未认证时 `_request_tenant()` 必须是 None（绝不凭空造租户）。"""
    from hero_quant.auth.context import bind_principal

    assert srv._request_tenant() is None
    with bind_principal(_principal("tenant-a")):
        assert srv._request_tenant() == "tenant-a"


def test_session_scope_returns_tenant_and_subject():
    from hero_quant.auth.context import bind_principal

    with bind_principal(_principal("tenant-a", "user-1")):
        assert srv._session_scope() == ("tenant-a", "user-1")


def test_session_scope_fail_closed_without_tenant():
    with pytest.raises(HTTPException) as exc:
        srv._session_scope()
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "auth_required_for_sessions"


# ---------------------------------------------------------------------------
# /v1/sessions* 端点层
# ---------------------------------------------------------------------------


class _FakeStore:
    """SessionStore 替身：记录调用参数，便于断言租户边界。"""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._raise = raise_exc

    def list_sessions(self, tenant, user_id=None, limit=50, offset=0):
        self.calls.append(("list", tenant, user_id, limit, offset))
        if self._raise:
            raise self._raise
        return [{"session_id": "s1", "tenant_id": tenant}]

    def get_session(self, tenant, session_id):
        self.calls.append(("get", tenant, session_id))
        if self._raise:
            raise self._raise
        return {"session_id": session_id, "tenant_id": tenant}

    def resume_session(self, tenant, session_id, after_id=0, limit=200):
        self.calls.append(("resume", tenant, session_id, after_id, limit))
        if self._raise:
            raise self._raise
        return {"session": {"session_id": session_id}, "last_event_id": 7}


@pytest.fixture
def sessions_client(monkeypatch):
    """把 _session_scope 与 SessionStore 换成替身，只测端点层的边界与映射。"""
    from hero_quant.session import store as store_mod

    store = _FakeStore()
    monkeypatch.setattr(srv, "_session_scope", lambda: ("tenant-a", "user-1"))
    monkeypatch.setattr(store_mod, "get_session_store", lambda: store)
    return TestClient(srv.app), store


def test_sessions_require_tenant_binding():
    """未绑定租户时会话接口 503 —— 绝不回退 default 租户。"""
    resp = TestClient(srv.app).get("/v1/sessions")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "auth_required_for_sessions"


def test_sessions_list_scoped_to_bound_tenant(sessions_client):
    client, store = sessions_client
    resp = client.get("/v1/sessions", params={"limit": 5, "offset": 10})
    assert resp.status_code == 200
    assert resp.json()["sessions"][0]["tenant_id"] == "tenant-a"
    assert store.calls == [("list", "tenant-a", "user-1", 5, 10)]


def test_sessions_get_and_resume_return_cursor(sessions_client):
    client, store = sessions_client
    assert client.get("/v1/sessions/s1").json()["session_id"] == "s1"
    body = client.post("/v1/sessions/s1/resume", params={"after_id": 3, "limit": 50}).json()
    assert body["session"]["session_id"] == "s1"
    assert body["last_event_id"] == 7, "续跑必须带回 SSE 游标"
    assert store.calls[-1] == ("resume", "tenant-a", "s1", 3, 50)


def test_sessions_error_mapping(monkeypatch, sessions_client):
    from hero_quant.session import store as store_mod

    client, _ = sessions_client
    monkeypatch.setattr(
        store_mod,
        "get_session_store",
        lambda: _FakeStore(raise_exc=store_mod.SessionNotFound("session_not_found")),
    )
    miss = client.get("/v1/sessions/nope")
    assert miss.status_code == 404
    assert miss.json()["detail"]["reason"] == "session_not_found"

    monkeypatch.setattr(
        store_mod,
        "get_session_store",
        lambda: _FakeStore(raise_exc=store_mod.SessionStoreUnavailable("pg_unreachable")),
    )
    down = client.get("/v1/sessions")
    assert down.status_code == 503
    assert down.json()["detail"]["reason"] == "pg_unreachable"


def test_llm_key_configured_detects_any_provider_key(monkeypatch):
    """无 key 时不做无谓的启动 ping；任一 provider key 存在才 ping。"""
    for key in ("HERO_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert srv._llm_key_configured() is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert srv._llm_key_configured() is True


def test_checkpoint_thread_survives_headerless_request():
    """回归：`_checkpoint_thread_for_request` 对无 headers 的替身不应抛错。"""
    headers = types.SimpleNamespace(get=lambda k, d="": d)
    assert srv._checkpoint_thread_for_request(types.SimpleNamespace(headers=headers)) == ("adhoc", "default")
