"""PR-241：GET /v1/audit/verify 端点 + 生产者 + 保留策略（离线契约，不碰真 PG）。

分工：
- 端点层只锁租户/角色闸、limit 夹取与错误映射（503/400/500），链的算法由
  tests/test_pr241_audit_chain.py 锁死、真库行为由 tests/test_pr241_audit_pg.py 锁死；
- 生产者锁「三个 action 常量 + 失败返回 None 且不外抛 + detail 不带 None 占位」；
- 保留策略锁「只读」：发出的 SQL 里不许出现 DELETE/UPDATE。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import structlog.contextvars as _cvars
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hero_quant.audit import api as audit_api
from hero_quant.audit import producers as producers_mod
from hero_quant.audit import retention as retention_mod
from hero_quant.audit.store import (
    AuditStoreUnavailable,
    AuditValidationError,
)
from hero_quant.auth.middleware import AuthContextMiddleware
from hero_quant.auth.oidc import AuthMode
from hero_quant.auth.principal import Role

_TENANT = "tenant-a"
_SUBJECT = "user-1"
_REPORT_KEYS = {
    "ok",
    "entries",
    "head_hash",
    "broken_at",
    "reason",
    "degraded",
    "reason_code",
    "chain_start_entry_id",
    "chain_start_at",
    "uncovered_count",
}


# ---------------------------------------------------------------------------
# 端点夹具
# ---------------------------------------------------------------------------
class _StubVerifier:
    def __init__(self, *roles: str) -> None:
        self._roles = list(roles)

    def verify(self, token: str) -> dict:
        return {"sub": _SUBJECT, "tenant_id": _TENANT, "roles": self._roles}


def _client(*roles: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(AuthContextMiddleware, verifier=_StubVerifier(*roles), mode=AuthMode.ENFORCE)
    app.include_router(audit_api.router)
    return TestClient(app)


class _FakeChain:
    def __init__(self, store) -> None:
        self.store = store
        type(self).instances.append(self)
        self.calls: list[dict] = []

    instances: list["_FakeChain"] = []

    def verify(self, tenant, *, limit=None):
        self.calls.append({"tenant": tenant, "limit": limit})
        return {
            "ok": True,
            "entries": 0,
            "head_hash": None,
            "broken_at": None,
            "reason": None,
            "degraded": False,
            "reason_code": None,
            "chain_start_entry_id": None,
            "chain_start_at": None,
            "uncovered_count": 0,
        }


@pytest.fixture(autouse=True)
def _isolate_contextvars():
    _cvars.clear_contextvars()
    _FakeChain.instances = []
    yield
    _cvars.clear_contextvars()


@pytest.fixture()
def chain_factory(monkeypatch):
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: object())
    monkeypatch.setattr(audit_api, "AuditChain", _FakeChain)
    return _FakeChain


# ---------------------------------------------------------------------------
# GET /v1/audit/verify
# ---------------------------------------------------------------------------
def test_verify_requires_admin(monkeypatch):
    monkeypatch.setattr(audit_api, "AuditChain", _FakeChain)
    resp = _client(Role.VIEWER.value).get("/v1/audit/verify", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "audit_requires_admin"
    assert _FakeChain.instances == [], "非 admin 不得触达校验"


def test_verify_without_tenant_is_503_and_never_touches_store(monkeypatch):
    def _explode():  # pragma: no cover - 被调用即失败
        raise AssertionError("无租户时不得触达审计存储")

    monkeypatch.setattr(audit_api, "get_audit_store", _explode)
    app = FastAPI()
    app.include_router(audit_api.router)
    resp = TestClient(app).get("/v1/audit/verify")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "auth_required_for_audit"


def test_verify_returns_full_report(chain_factory):
    resp = _client(Role.ADMIN.value).get("/v1/audit/verify", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _REPORT_KEYS
    assert chain_factory.instances[0].calls == [{"tenant": _TENANT, "limit": None}]


def test_verify_clamps_limit(chain_factory):
    client = _client(Role.ADMIN.value)
    client.get("/v1/audit/verify", params={"limit": 9999}, headers={"Authorization": "Bearer t"})
    client.get("/v1/audit/verify", params={"limit": 0}, headers={"Authorization": "Bearer t"})
    # 每个请求新建一条链实例，按请求顺序摊平取 limit
    limits = [call["limit"] for chain in chain_factory.instances for call in chain.calls]
    assert limits == [audit_api.MAX_LIMIT, 1]


def test_verify_maps_validation_error_to_400(chain_factory, monkeypatch):
    class _Bad(_FakeChain):
        def verify(self, tenant, *, limit=None):
            raise AuditValidationError("limit_must_be_int")

    monkeypatch.setattr(audit_api, "AuditChain", _Bad)
    resp = _client(Role.ADMIN.value).get("/v1/audit/verify", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["detail"] == "limit_must_be_int"


def test_verify_maps_store_unavailable_to_503(chain_factory, monkeypatch):
    class _Down(_FakeChain):
        def verify(self, tenant, *, limit=None):
            raise AuditStoreUnavailable("pg_unreachable")

    monkeypatch.setattr(audit_api, "AuditChain", _Down)
    resp = _client(Role.ADMIN.value).get("/v1/audit/verify", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "audit_store_unavailable"


def test_verify_maps_unexpected_error_to_generic_500(chain_factory, monkeypatch):
    class _Boom(_FakeChain):
        def verify(self, tenant, *, limit=None):
            raise RuntimeError("driver exploded with secret detail")

    monkeypatch.setattr(audit_api, "AuditChain", _Boom)
    resp = _client(Role.ADMIN.value).get("/v1/audit/verify", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 500
    assert resp.json()["detail"] == {"reason": "audit_error"}
    assert "secret" not in resp.text, "异常原文不得回显"


# ---------------------------------------------------------------------------
# producers
# ---------------------------------------------------------------------------
class _FakeProducerChain:
    instances: list["_FakeProducerChain"] = []

    def __init__(self, store) -> None:
        self.store = store
        self.appends: list[dict] = []
        type(self).instances.append(self)

    def append(self, tenant, action, **fields):
        self.appends.append({"tenant": tenant, "action": action, **fields})
        return {"id": 1, "action": action, "entry_hash": "e" * 64, **fields}


@pytest.fixture()
def producer_chain(monkeypatch):
    _FakeProducerChain.instances = []
    monkeypatch.setattr(producers_mod, "get_audit_store", lambda: object())
    monkeypatch.setattr(producers_mod, "AuditChain", _FakeProducerChain)
    return _FakeProducerChain


def test_action_constants_are_fixed():
    assert producers_mod.ACTION_TOOL_CALL == "tool.call"
    assert producers_mod.ACTION_BACKTEST_RUN == "backtest.run"
    assert producers_mod.ACTION_SESSION_RUN == "session.run"


def test_record_tool_call_audit_builds_detail_without_none(producer_chain):
    row = producers_mod.record_tool_call_audit(
        _TENANT,
        tool="quote",
        run_id="run-1",
        user_id=_SUBJECT,
        ok=False,
        duration_ms=12,
        error_code="upstream_timeout",
        detail={"attempt": 1, "cache": None},
    )
    assert row is not None and row["action"] == "tool.call"
    call = producer_chain.instances[0].appends[0]
    assert call["tenant"] == _TENANT and call["resource"] == "quote"
    assert call["detail"] == {
        "attempt": 1,
        "ok": False,
        "duration_ms": 12,
        "error_code": "upstream_timeout",
    }


def test_record_backtest_and_session_audit_use_their_actions(producer_chain):
    producers_mod.record_backtest_audit(_TENANT, run_id="r1", strategy="ma_cross", ok=True)
    producers_mod.record_session_audit(_TENANT, run_id="r2", session_id="s1", ok=True)
    calls = [call for chain in producer_chain.instances for call in chain.appends]
    assert [call["action"] for call in calls] == ["backtest.run", "session.run"]
    assert [call["resource"] for call in calls] == ["ma_cross", "s1"]


def test_producer_returns_none_without_tenant(monkeypatch):
    def _explode():  # pragma: no cover - 被调用即失败
        raise AssertionError("无租户不得触达存储")

    monkeypatch.setattr(producers_mod, "get_audit_store", _explode)
    assert producers_mod.record_tool_call_audit("", tool="quote") is None
    assert producers_mod.record_tool_call_audit(None, tool="quote") is None


def test_producer_swallows_store_failure(monkeypatch):
    def _down():
        raise AuditStoreUnavailable("pg_unreachable")

    monkeypatch.setattr(producers_mod, "get_audit_store", _down)
    assert producers_mod.record_tool_call_audit(_TENANT, tool="quote") is None


def test_producer_swallows_unexpected_append_error(monkeypatch):
    class _Boom:
        def __init__(self, store) -> None:
            pass

        def append(self, *args, **kwargs):
            raise RuntimeError("driver exploded")

    monkeypatch.setattr(producers_mod, "get_audit_store", lambda: object())
    monkeypatch.setattr(producers_mod, "AuditChain", _Boom)
    assert producers_mod.record_backtest_audit(_TENANT, run_id="r1") is None


# ---------------------------------------------------------------------------
# retention：只读规划
# ---------------------------------------------------------------------------
class _FakeRetentionStore:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    @contextmanager
    def _tx(self, tenant):
        yield object()


def test_retention_plan_is_read_only(monkeypatch):
    store = _FakeRetentionStore()
    captured: list[str] = []

    def _fetch_one(conn, sql, params=None):
        captured.append(sql)
        assert "count(*)" in sql
        return {
            "total_count": 10,
            "archivable_count": 3,
            "oldest_at": "2025-01-01T00:00:00.000000+00:00",
            "newest_at": "2026-10-01T00:00:00.000000+00:00",
        }

    monkeypatch.setattr(retention_mod, "get_audit_store", lambda: store)
    monkeypatch.setattr(retention_mod, "fetch_one", _fetch_one)
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    plan = retention_mod.retention_plan(_TENANT, older_than_days=365, now=now)

    assert plan["cutoff"] == (now - timedelta(days=365)).isoformat()
    assert plan["archivable_count"] == 3 and plan["total_count"] == 10
    assert plan["delete_performed"] is False
    assert plan["manual_archive_required"] is True
    assert captured, "必须真的查询过"
    for sql in captured:
        upper = sql.upper()
        assert "DELETE" not in upper and "UPDATE" not in upper and "TRUNCATE" not in upper


def test_retention_plan_default_keeps_one_year(monkeypatch):
    monkeypatch.setattr(retention_mod, "get_audit_store", lambda: _FakeRetentionStore())
    monkeypatch.setattr(retention_mod, "fetch_one", lambda conn, sql, params=None: {})
    plan = retention_mod.retention_plan(_TENANT)
    assert plan["older_than_days"] == 365
    assert plan["total_count"] == 0 and plan["archivable_count"] == 0


@pytest.mark.parametrize("bad", [-1, 36_501, True, "365"])
def test_retention_plan_rejects_bad_window(bad):
    with pytest.raises(AuditValidationError):
        retention_mod.retention_plan(_TENANT, older_than_days=bad)
