"""PR-215：管理后台 API（离线契约，不碰真 PG/Redis）。

分工：
- 端点层锁 admin 闸 / 租户闸 / 降级语义（字段 null + degraded + reason）/ 错误映射；
- `AdminStore` 用假池锁 SQL 形状：跨租户先查 pg_roles、单租户设事务级 GUC；
- 数据源一律替身（`admin_api.get_*_store` / `get_run_queue` / `get_kill_switch`），
  真库行为由 `tests/test_pr215_admin_pg.py` 在 PG 守卫下验证。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
import structlog.contextvars as _cvars
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hero_quant.admin import api as admin_api
from hero_quant.admin import store as admin_store_mod
from hero_quant.admin.store import AdminScopeUnavailable, AdminStore, AdminStoreUnavailable
from hero_quant.auth.middleware import AuthContextMiddleware
from hero_quant.auth.oidc import AuthMode
from hero_quant.auth.principal import Role
from hero_quant.auth.quota import QuotaPolicy
from hero_quant.db.pool import DatabaseUnavailable
from hero_quant.killswitch.switch import KillSwitchUnavailable, KillSwitchValidationError
from hero_quant.runs.store import RunsUnavailable, RunsValidationError

_TENANT = "tenant-a"
_SUBJECT = "user-1"

_ENDPOINTS = ("/overview", "/tenants", "/runs", "/quotas", "/audit/summary", "/toggles")

_CHAIN_REPORT = {
    "ok": True,
    "entries": 4,
    "head_hash": "a" * 64,
    "broken_at": None,
    "reason": None,
    "degraded": False,
    "reason_code": None,
    "chain_start_entry_id": 10,
    "chain_start_at": "2026-11-01T00:00:00+00:00",
    "uncovered_count": 3,
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class _StubVerifier:
    def __init__(self, *roles: str) -> None:
        self._roles = list(roles)

    def verify(self, token: str) -> dict:
        return {"sub": _SUBJECT, "tenant_id": _TENANT, "roles": self._roles}


def _client(*roles: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(AuthContextMiddleware, verifier=_StubVerifier(*roles), mode=AuthMode.ENFORCE)
    app.include_router(admin_api.router)
    return TestClient(app)


class _FakeAdminStore:
    def __init__(
        self,
        *,
        overview: dict | None = None,
        overview_exc: Exception | None = None,
        tenants: list[dict] | None = None,
        tenants_exc: Exception | None = None,
        audit_total: int = 0,
    ) -> None:
        self._overview = overview
        self._overview_exc = overview_exc
        self._tenants = list(tenants or [])
        self._tenants_exc = tenants_exc
        self._audit_total = audit_total
        self.overview_calls: list[str | None] = []
        self.tenant_calls: list[int] = []

    def overview(self, tenant_id):
        self.overview_calls.append(tenant_id)
        if self._overview_exc is not None:
            raise self._overview_exc
        return self._overview

    def tenants(self, *, limit):
        self.tenant_calls.append(limit)
        if self._tenants_exc is not None:
            raise self._tenants_exc
        return list(self._tenants)

    def audit_total(self, tenant_id):
        return self._audit_total


class _FakeRunQueue:
    def __init__(self, *, rows: list[dict] | None = None, exc: Exception | None = None) -> None:
        self._rows = list(rows or [])
        self._exc = exc
        self.calls: list[dict] = []

    def list(self, tenant, *, status=None, limit=50):
        self.calls.append({"tenant": tenant, "status": status, "limit": limit})
        if self._exc is not None:
            raise self._exc
        return list(self._rows)


class _FakeKillSwitch:
    def __init__(
        self,
        *,
        rows: list[dict] | None = None,
        list_exc: Exception | None = None,
        enable_exc: Exception | None = None,
        disable_exc: Exception | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self._list_exc = list_exc
        self._enable_exc = enable_exc
        self._disable_exc = disable_exc
        self.calls: list[dict] = []

    async def list_active(self, scope=None):
        self.calls.append({"op": "list", "scope": scope})
        if self._list_exc is not None:
            raise self._list_exc
        return list(self._rows)

    async def enable(self, scope, target, reason, *, actor, tenant_id=None, ttl_seconds=None):
        self.calls.append(
            {
                "op": "enable",
                "scope": scope,
                "target": target,
                "reason": reason,
                "actor": actor,
                "tenant_id": tenant_id,
            }
        )
        if self._enable_exc is not None:
            raise self._enable_exc
        return {"scope": scope, "target": target, "reason": reason, "actor": actor}

    async def disable(self, scope, target, *, actor, tenant_id=None):
        self.calls.append(
            {"op": "disable", "scope": scope, "target": target, "actor": actor, "tenant_id": tenant_id}
        )
        if self._disable_exc is not None:
            raise self._disable_exc
        return True


class _FakeAuditStore:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = list(rows or [])
        self.query_calls: list[dict] = []

    def query(self, tenant_id, *, limit=100, **kwargs):
        self.query_calls.append({"tenant": tenant_id, "limit": limit, **kwargs})
        return list(self._rows)


class _FakeChain:
    instances: list["_FakeChain"] = []

    def __init__(self, store, *, append_exc: Exception | None = None) -> None:
        self._append_exc = append_exc
        self.appends: list[dict] = []
        self.verify_calls: list[str] = []
        type(self).instances.append(self)

    def append(self, tenant, action, **fields):
        self.appends.append({"tenant": tenant, "action": action, **fields})
        if self._append_exc is not None:
            raise self._append_exc
        return {"id": 1, "entry_hash": "f" * 64}

    def verify(self, tenant, *, limit=None):
        self.verify_calls.append(tenant)
        return dict(_CHAIN_REPORT)


class _FakeLimiter:
    def __init__(self, *, used: int = 0) -> None:
        self.policy = QuotaPolicy(requests_per_minute=60, daily_token_budget=1000, window_seconds=60)
        self._used = used
        self.usage_calls: list[str] = []

    async def usage(self, tenant_id):
        self.usage_calls.append(tenant_id)
        return self._used


class _FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def ping(self):
        if self._fail:
            raise ConnectionError("redis down")
        return True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _cvars.clear_contextvars()
    _FakeChain.instances = []
    monkeypatch.setattr(admin_api, "get_audit_store", lambda: object())
    monkeypatch.setattr(admin_api, "AuditChain", _FakeChain)
    yield
    _cvars.clear_contextvars()


def _patch_store(monkeypatch, store) -> None:
    monkeypatch.setattr(admin_api, "get_admin_store", lambda: store)


# ---------------------------------------------------------------------------
# 鉴权闸
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", _ENDPOINTS)
def test_non_admin_gets_403(path, monkeypatch):
    store = _FakeAdminStore()
    _patch_store(monkeypatch, store)
    method = "post" if path == "/toggles" else "get"
    kwargs = {"json": {"scope": "model", "target": "m", "action": "enable", "reason": "r"}} if method == "post" else {}
    resp = getattr(_client(Role.VIEWER.value), method)(path, headers={"Authorization": "Bearer t"}, **kwargs)
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "admin_role_required"
    assert store.overview_calls == [], "非 admin 不得触达数据层"


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_missing_tenant_gets_503(path):
    app = FastAPI()
    app.include_router(admin_api.router)
    method = "post" if path == "/toggles" else "get"
    kwargs = {"json": {"scope": "model", "target": "m", "action": "enable", "reason": "r"}} if method == "post" else {}
    resp = getattr(TestClient(app), method)(path, **kwargs)
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "auth_required_for_admin"


# ---------------------------------------------------------------------------
# GET /overview
# ---------------------------------------------------------------------------
def _overview_payload(scope: str) -> dict:
    return {
        "runs": {"total": 5, "by_status": {"queued": 2, "running": 1, "succeeded": 1, "failed": 1}},
        "audit": {"entries": 7},
        "queue": {
            "oldest_queued_run_id": "run-1",
            "oldest_queued_created_at": "2026-11-01T00:00:00+00:00",
            "oldest_queued_wait_seconds": 12.5,
        },
    }


def test_overview_platform_counts_fill_missing_statuses(monkeypatch):
    store = _FakeAdminStore(overview=_overview_payload("platform"))
    _patch_store(monkeypatch, store)
    resp = _client(Role.ADMIN.value).get("/overview", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "platform"
    assert body["tenant_id"] == _TENANT
    assert body["runs"]["total"] == 5
    assert body["runs"]["by_status"]["queued"] == 2
    assert body["runs"]["by_status"]["dead"] == 0
    assert body["audit"] == {"entries": 7}
    assert body["queue"]["oldest_queued_wait_seconds"] == 12.5
    assert body["degraded"] is False
    assert store.overview_calls == [None]


def test_overview_degrades_to_tenant_scope_when_cross_tenant_denied(monkeypatch):
    class _Scoped(_FakeAdminStore):
        def overview(self, tenant_id):
            self.overview_calls.append(tenant_id)
            if tenant_id is None:
                raise AdminScopeUnavailable("cross_tenant_not_permitted")
            return _overview_payload("tenant")

    store = _Scoped()
    _patch_store(monkeypatch, store)
    body = _client(Role.ADMIN.value).get("/overview", headers={"Authorization": "Bearer t"}).json()
    assert body["scope"] == "tenant"
    assert body["degraded"] is True
    assert body["reason"] == "cross_tenant_not_permitted"
    assert body["reasons"] == ["cross_tenant_not_permitted"]
    assert store.overview_calls == [None, _TENANT]


def test_overview_pg_down_returns_nulls_not_zero(monkeypatch):
    _patch_store(monkeypatch, _FakeAdminStore(overview_exc=AdminStoreUnavailable("pg_unavailable")))
    body = _client(Role.ADMIN.value).get("/overview", headers={"Authorization": "Bearer t"}).json()
    assert body["runs"] is None and body["audit"] is None and body["queue"] is None
    assert body["degraded"] is True
    assert body["reason"] == "pg_unavailable"


# ---------------------------------------------------------------------------
# GET /tenants
# ---------------------------------------------------------------------------
def test_tenants_lists_rows_and_clamps_limit(monkeypatch):
    rows = [
        {"tenant_id": "tenant-a", "runs": 3, "last_activity_at": "2026-11-01T00:00:00+00:00"},
        {"tenant_id": "tenant-b", "runs": 0, "last_activity_at": None},
    ]
    store = _FakeAdminStore(tenants=rows)
    _patch_store(monkeypatch, store)
    body = _client(Role.ADMIN.value).get(
        "/tenants", params={"limit": 9999}, headers={"Authorization": "Bearer t"}
    ).json()
    assert body["count"] == 2
    assert body["limit"] == admin_api.MAX_TENANTS_LIMIT
    assert [r["tenant_id"] for r in body["tenants"]] == ["tenant-a", "tenant-b"]
    assert store.tenant_calls == [admin_api.MAX_TENANTS_LIMIT]


def test_tenants_scope_denied_is_null_not_empty(monkeypatch):
    _patch_store(
        monkeypatch, _FakeAdminStore(tenants_exc=AdminScopeUnavailable("cross_tenant_not_permitted"))
    )
    body = _client(Role.ADMIN.value).get("/tenants", headers={"Authorization": "Bearer t"}).json()
    assert body["tenants"] is None and body["count"] is None
    assert body["degraded"] is True
    assert body["reason"] == "cross_tenant_not_permitted"


def test_tenants_pg_down_degrades(monkeypatch):
    _patch_store(monkeypatch, _FakeAdminStore(tenants_exc=AdminStoreUnavailable("pg_unavailable")))
    body = _client(Role.ADMIN.value).get("/tenants", headers={"Authorization": "Bearer t"}).json()
    assert body["tenants"] is None
    assert body["reason"] == "pg_unavailable"


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------
def test_runs_passes_filters_and_clamps_limit(monkeypatch):
    queue = _FakeRunQueue(rows=[{"id": "run-1", "status": "queued"}])
    monkeypatch.setattr(admin_api, "get_run_queue", lambda: queue)
    body = _client(Role.ADMIN.value).get(
        "/runs", params={"status": "queued", "limit": 500}, headers={"Authorization": "Bearer t"}
    ).json()
    assert body["count"] == 1
    assert body["limit"] == admin_api.MAX_RUNS_LIMIT
    assert body["tenant_id"] == _TENANT
    assert queue.calls == [
        {"tenant": _TENANT, "status": "queued", "limit": admin_api.MAX_RUNS_LIMIT}
    ]


def test_runs_invalid_status_400(monkeypatch):
    monkeypatch.setattr(
        admin_api, "get_run_queue", lambda: _FakeRunQueue(exc=RunsValidationError("status_not_supported"))
    )
    resp = _client(Role.ADMIN.value).get(
        "/runs", params={"status": "bogus"}, headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == {"reason": "runs_invalid", "detail": "status_not_supported"}


def test_runs_unavailable_degrades(monkeypatch):
    monkeypatch.setattr(admin_api, "get_run_queue", lambda: _FakeRunQueue(exc=RunsUnavailable("pg_down")))
    body = _client(Role.ADMIN.value).get("/runs", headers={"Authorization": "Bearer t"}).json()
    assert body["runs"] is None and body["count"] is None
    assert body["reason"] == "runs_unavailable"


# ---------------------------------------------------------------------------
# GET /quotas
# ---------------------------------------------------------------------------
def test_quotas_returns_policy_and_usage(monkeypatch):
    limiter = _FakeLimiter(used=250)
    monkeypatch.setattr(admin_api, "default_quota", lambda: limiter)

    async def _redis():
        return _FakeRedis()

    monkeypatch.setattr(admin_api, "get_redis", _redis)
    body = _client(Role.ADMIN.value).get("/quotas", headers={"Authorization": "Bearer t"}).json()
    assert body["policy"] == {
        "requests_per_minute": 60,
        "window_seconds": 60,
        "daily_token_budget": 1000,
    }
    assert body["tokens_used"] == 250
    assert body["tokens_remaining"] == 750
    assert body["usage_ratio"] == pytest.approx(0.25)
    assert body["unlimited"] is False
    assert body["rate_window_used"] is None
    assert body["rate_window_reason"] == "rate_window_not_exposed"
    assert body["degraded"] is False
    assert limiter.usage_calls == [_TENANT]


def test_quotas_redis_down_returns_null(monkeypatch):
    monkeypatch.setattr(admin_api, "default_quota", lambda: _FakeLimiter(used=250))

    async def _redis():
        return None

    monkeypatch.setattr(admin_api, "get_redis", _redis)
    body = _client(Role.ADMIN.value).get("/quotas", headers={"Authorization": "Bearer t"}).json()
    assert body["tokens_used"] is None
    assert body["tokens_remaining"] is None
    assert body["usage_ratio"] is None
    assert body["degraded"] is True
    assert body["reason"] == "redis_unavailable"


def test_quotas_ping_failure_returns_null(monkeypatch):
    monkeypatch.setattr(admin_api, "default_quota", lambda: _FakeLimiter(used=250))

    async def _redis():
        return _FakeRedis(fail=True)

    monkeypatch.setattr(admin_api, "get_redis", _redis)
    body = _client(Role.ADMIN.value).get("/quotas", headers={"Authorization": "Bearer t"}).json()
    assert body["tokens_used"] is None
    assert body["reason"] == "redis_unavailable"


# ---------------------------------------------------------------------------
# GET /audit/summary
# ---------------------------------------------------------------------------
def test_audit_summary_strips_detail_and_returns_chain(monkeypatch):
    audit_store = _FakeAuditStore(
        rows=[
            {
                "id": 11,
                "ts": "2026-11-01T00:00:00+00:00",
                "user_id": _SUBJECT,
                "action": "query.completed",
                "resource": "query",
                "run_id": "trace-1",
                "model": "m",
                "prompt_hash": "p" * 32,
                "result_hash": None,
                "detail": {"prompt": "should never leave the API"},
            }
        ]
    )
    monkeypatch.setattr(admin_api, "get_audit_store", lambda: audit_store)
    _patch_store(monkeypatch, _FakeAdminStore(audit_total=9))
    body = _client(Role.ADMIN.value).get(
        "/audit/summary", params={"limit": 500}, headers={"Authorization": "Bearer t"}
    ).json()
    assert body["entries"] == 9
    assert body["recent_limit"] == admin_api.MAX_AUDIT_LIMIT
    assert body["recent"][0]["action"] == "query.completed"
    assert "detail" not in body["recent"][0]
    assert body["chain"] == _CHAIN_REPORT
    assert audit_store.query_calls == [{"tenant": _TENANT, "limit": admin_api.MAX_AUDIT_LIMIT}]
    assert _FakeChain.instances[0].verify_calls == [_TENANT]


def test_audit_summary_store_unavailable_degrades(monkeypatch):
    _patch_store(monkeypatch, _FakeAdminStore(audit_total=0))

    def _boom() -> Any:
        raise admin_api.AuditStoreUnavailable("pg_unreachable")

    monkeypatch.setattr(admin_api, "get_audit_store", _boom)
    body = _client(Role.ADMIN.value).get("/audit/summary", headers={"Authorization": "Bearer t"}).json()
    assert body["entries"] is None and body["recent"] is None and body["chain"] is None
    assert body["degraded"] is True
    assert body["reason"] == "audit_store_unavailable"


# ---------------------------------------------------------------------------
# GET /toggles
# ---------------------------------------------------------------------------
def test_toggles_lists_active(monkeypatch):
    switch = _FakeKillSwitch(rows=[{"scope": "model", "target": "m", "reason": "incident"}])
    monkeypatch.setattr(admin_api, "get_kill_switch", lambda: switch)
    body = _client(Role.ADMIN.value).get("/toggles", headers={"Authorization": "Bearer t"}).json()
    assert body["count"] == 1
    assert body["toggles"][0]["target"] == "m"
    assert switch.calls == [{"op": "list", "scope": None}]


def test_toggles_redis_down_degrades(monkeypatch):
    monkeypatch.setattr(
        admin_api, "get_kill_switch", lambda: _FakeKillSwitch(list_exc=KillSwitchUnavailable("redis_unavailable"))
    )
    body = _client(Role.ADMIN.value).get("/toggles", headers={"Authorization": "Bearer t"}).json()
    assert body["toggles"] is None
    assert body["reason"] == "killswitch_unavailable"


# ---------------------------------------------------------------------------
# POST /toggles
# ---------------------------------------------------------------------------
_TOGGLE_BODY = {"scope": "model", "target": "m-1", "action": "enable", "reason": "incident-42"}


def test_toggle_enable_scopes_to_tenant_and_audits(monkeypatch):
    switch = _FakeKillSwitch()
    monkeypatch.setattr(admin_api, "get_kill_switch", lambda: switch)
    body = _client(Role.ADMIN.value).post(
        "/toggles", json=_TOGGLE_BODY, headers={"Authorization": "Bearer t"}
    ).json()
    assert body["ok"] is True and body["changed"] is True
    assert body["tenant_id"] == _TENANT
    assert body["audit_recorded"] is True
    assert switch.calls == [
        {
            "op": "enable",
            "scope": "model",
            "target": "m-1",
            "reason": "incident-42",
            "actor": _SUBJECT,
            "tenant_id": _TENANT,
        }
    ]
    append = _FakeChain.instances[0].appends[0]
    assert append["tenant"] == _TENANT
    assert append["action"] == "admin.toggle"
    assert append["user_id"] == _SUBJECT
    assert append["detail"] == {
        "scope": "model",
        "target": "m-1",
        "action": "enable",
        "reason": "incident-42",
    }


def test_toggle_tenant_scope_does_not_prefix_tenant(monkeypatch):
    switch = _FakeKillSwitch()
    monkeypatch.setattr(admin_api, "get_kill_switch", lambda: switch)
    body = _client(Role.ADMIN.value).post(
        "/toggles",
        json={"scope": "tenant", "target": "tenant-b", "action": "disable", "reason": "restore"},
        headers={"Authorization": "Bearer t"},
    ).json()
    assert body["tenant_id"] is None
    assert switch.calls[0]["tenant_id"] is None
    assert switch.calls[0]["op"] == "disable"


def test_toggle_invalid_action_400_and_never_calls_switch(monkeypatch):
    switch = _FakeKillSwitch()
    monkeypatch.setattr(admin_api, "get_kill_switch", lambda: switch)
    resp = _client(Role.ADMIN.value).post(
        "/toggles",
        json={**_TOGGLE_BODY, "action": "toggle"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "toggle_action_invalid"
    assert switch.calls == []


def test_toggle_invalid_scope_400(monkeypatch):
    monkeypatch.setattr(
        admin_api,
        "get_kill_switch",
        lambda: _FakeKillSwitch(enable_exc=KillSwitchValidationError("scope_must_be_one_of:model,tool,tenant")),
    )
    resp = _client(Role.ADMIN.value).post(
        "/toggles", json={**_TOGGLE_BODY, "scope": "planet"}, headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "killswitch_invalid"


def test_toggle_redis_down_503(monkeypatch):
    monkeypatch.setattr(
        admin_api,
        "get_kill_switch",
        lambda: _FakeKillSwitch(enable_exc=KillSwitchUnavailable("redis_set_failed")),
    )
    resp = _client(Role.ADMIN.value).post(
        "/toggles", json=_TOGGLE_BODY, headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "killswitch_unavailable"


def test_toggle_audit_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(admin_api, "get_kill_switch", lambda: _FakeKillSwitch())

    class _BadChain(_FakeChain):
        def append(self, tenant, action, **fields):
            raise RuntimeError("pg down with secret detail")

    monkeypatch.setattr(admin_api, "AuditChain", _BadChain)
    resp = _client(Role.ADMIN.value).post(
        "/toggles", json=_TOGGLE_BODY, headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["audit_recorded"] is False
    assert body["degraded"] is True
    assert body["reason"] == "audit_append_failed"
    assert "secret" not in resp.text


def test_toggle_unexpected_error_500_without_echo(monkeypatch):
    monkeypatch.setattr(
        admin_api, "get_kill_switch", lambda: _FakeKillSwitch(enable_exc=RuntimeError("secret detail"))
    )
    resp = _client(Role.ADMIN.value).post(
        "/toggles", json=_TOGGLE_BODY, headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == {"reason": "admin_error"}
    assert "secret" not in resp.text


# ---------------------------------------------------------------------------
# AdminStore（假池）：SQL 形状与 GUC
# ---------------------------------------------------------------------------
class _Cursor:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.description = [type("Column", (), {"name": name}) for name in columns]
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, results: list[tuple[str, list[str], list[tuple]]], executed: list[tuple]) -> None:
        self._results = results
        self.executed = executed

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for needle, columns, rows in self._results:
            if needle in sql:
                return _Cursor(columns, rows)
        return _Cursor([], [])

    @contextmanager
    def transaction(self):
        yield self


class _FakePool:
    def __init__(self, results: list[tuple[str, list[str], list[tuple]]]) -> None:
        self.executed: list[tuple] = []
        self._results = results

    @contextmanager
    def connection(self):
        yield _FakeConn(self._results, self.executed)


def _results(*, cross_tenant: bool = True) -> list[tuple[str, list[str], list[tuple]]]:
    return [
        ("pg_roles", ["allowed"], [(cross_tenant,)]),
        ("GROUP BY status", ["status", "count"], [("queued", 2), ("running", 1)]),
        ("count(*) AS total", ["total"], [(7,)]),
        (
            "status = 'queued'",
            ["id", "created_at", "wait_seconds"],
            [("run-1", "2026-11-01T00:00:00+00:00", 12.5)],
        ),
        (
            "last_activity_at",
            ["tenant_id", "runs", "last_activity_at"],
            [("tenant-a", 3, "2026-11-01T00:00:00+00:00")],
        ),
    ]


def test_admin_store_platform_overview_checks_role_and_skips_guc():
    pool = _FakePool(_results(cross_tenant=True))
    data = AdminStore(pool).overview(None)
    assert data["runs"] == {"total": 3, "by_status": {"queued": 2, "running": 1}}
    assert data["audit"] == {"entries": 7}
    assert data["queue"]["oldest_queued_wait_seconds"] == 12.5
    sqls = [sql for sql, _ in pool.executed]
    assert any("pg_roles" in sql for sql in sqls)
    assert not any("set_config" in sql for sql in sqls)


def test_admin_store_tenant_overview_sets_transaction_local_guc():
    pool = _FakePool(_results(cross_tenant=True))
    data = AdminStore(pool).overview(_TENANT)
    assert data["runs"]["by_status"]["queued"] == 2
    config_calls = [(sql, params) for sql, params in pool.executed if "set_config" in sql]
    assert [params for _, params in config_calls] == [(_TENANT,), (_TENANT,)]
    assert all("true" in sql for sql, _ in config_calls), "必须是 SET LOCAL 语义"
    assert not any("pg_roles" in sql for sql, _ in pool.executed)


def test_admin_store_denies_when_role_cannot_cross_tenant():
    pool = _FakePool(_results(cross_tenant=False))
    with pytest.raises(AdminScopeUnavailable):
        AdminStore(pool).overview(None)
    assert not any("GROUP BY status" in sql for sql, _ in pool.executed), "无权限时不得继续查数据"


def test_admin_store_tenants_returns_rows():
    pool = _FakePool(_results())
    rows = AdminStore(pool).tenants(limit=10)
    assert rows == [
        {"tenant_id": "tenant-a", "runs": 3, "last_activity_at": "2026-11-01T00:00:00+00:00"}
    ]
    params = [params for sql, params in pool.executed if "last_activity_at" in sql][0]
    assert params == (10,)


def test_admin_store_maps_missing_pool_to_unavailable(monkeypatch):
    def _no_pool():
        raise DatabaseUnavailable("checkpoint_dsn_not_configured")

    monkeypatch.setattr(admin_store_mod, "get_pool", _no_pool)
    with pytest.raises(AdminStoreUnavailable) as exc:
        AdminStore.from_settings()
    assert exc.value.reason == "pg_unavailable"
