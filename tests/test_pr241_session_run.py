"""批次16：会话/运行生命周期 → 审计链（session.run）的离线口径测试。

真链入链/历史行不误判由 tests/test_pr241_session_pg.py 覆盖（需要真 PG）。
"""

from __future__ import annotations

import pytest

from hero_quant.audit import producers
from hero_quant.audit.producers import session_run_hook
from hero_quant.session import store as session_store_mod


def _capture(monkeypatch):
    calls = []

    def _fake_record(tenant_id, **kwargs):
        calls.append({"tenant_id": tenant_id, **kwargs})
        return {"id": 1}

    monkeypatch.setattr(producers, "record_session_audit", _fake_record)
    return calls


def test_hook_maps_lifecycle_phases(monkeypatch):
    calls = _capture(monkeypatch)
    hook = session_run_hook()

    hook({"phase": "created", "tenant_id": "t1", "session_id": "s1"})
    hook({"phase": "started", "tenant_id": "t1", "session_id": "s1", "run_id": "r1"})
    hook({"phase": "finished", "tenant_id": "t1", "session_id": "s1", "run_id": "r1", "status": "succeeded"})
    hook({"phase": "finished", "tenant_id": "t1", "session_id": "s1", "run_id": "r2", "status": "failed"})

    assert [c["detail"]["phase"] for c in calls] == ["created", "started", "finished", "finished"]
    assert [c["ok"] for c in calls] == [True, True, True, False]
    assert calls[3]["error_code"] == "failed"
    assert calls[1]["run_id"] == "r1"
    # detail 只允许 phase/status，绝不带消息正文
    assert all(set(c["detail"]) <= {"phase", "status"} for c in calls)


def test_hook_swallows_errors(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(producers, "record_session_audit", _boom)
    hook = session_run_hook()
    hook({"phase": "started", "tenant_id": "t1", "session_id": "s1"})  # 不抛即通过


def test_missing_tenant_is_passed_through_as_none(monkeypatch):
    calls = _capture(monkeypatch)
    session_run_hook()({"phase": "created", "session_id": "s1"})
    assert calls[0]["tenant_id"] is None  # 由 producers 侧统一跳过，不在钩子里编租户


def test_factory_wires_default_audit_hook(monkeypatch):
    captured = {}

    def _fake_from_dsn(dsn=None, *, audit_hook=None):
        captured["audit_hook"] = audit_hook
        return object()

    monkeypatch.setattr(session_store_mod.SessionStore, "from_dsn", classmethod(lambda cls, dsn=None, *, audit_hook=None: _fake_from_dsn(dsn, audit_hook=audit_hook)))
    session_store_mod.reset_session_store()
    try:
        session_store_mod.get_session_store()
        assert callable(captured["audit_hook"]), "工厂必须注入会话审计钩子"
    finally:
        session_store_mod.reset_session_store()
