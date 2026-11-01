"""PR-241 补充：query.* 审计并入链（api._append 走 AuditChain）。

离线只锁三件事（真库的 entry_hash/覆盖口径见 tests/test_pr241_audit_pg.py）：
1. _append 走链式追加，且**显式**传 tz-aware UTC 的 ts（hash 含 ts，交给 DB now() 必失配）；
2. 字段与改造前完全一致（user_id/resource/run_id/model/prompt_hash/detail）；
3. 失败语义不变：链写异常只降级成 False + warning，绝不外抛。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from hero_quant.audit import api as audit_api
from hero_quant.audit.store import AuditStoreUnavailable

_TENANT = "tenant-a"


def _fields() -> dict[str, Any]:
    return {
        "user_id": "user-1",
        "resource": "/v1/query",
        "run_id": "run-1",
        "model": "model-x",
        "prompt_hash": "p" * 32,
        "detail": {"tokens": {"total_tokens": 3}, "grounded": True},
    }


class _ChainRecorder:
    """AuditChain 替身：记录构造与 append 参数（不碰 PG）。"""

    instances: list["_ChainRecorder"] = []

    def __init__(self, store) -> None:
        self.store = store
        self.appends: list[dict] = []
        type(self).instances.append(self)

    def append(self, tenant, action, **fields):
        record = {"tenant": tenant, "action": action, **fields}
        self.appends.append(record)
        return {"id": 1, "entry_hash": "e" * 64}


@pytest.fixture(autouse=True)
def _reset_recorder():
    _ChainRecorder.instances = []
    yield
    _ChainRecorder.instances = []


@pytest.fixture()
def chained(monkeypatch):
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: object())
    monkeypatch.setattr(audit_api, "AuditChain", _ChainRecorder)
    return _ChainRecorder


def test_append_goes_through_chain_with_explicit_utc_ts(chained):
    before = datetime.now(timezone.utc)

    ok = audit_api._append(_TENANT, "query.completed", **_fields())

    assert ok is True
    assert len(chained.instances) == 1, "必须实例化 AuditChain（而不是 store.try_append）"
    call = chained.instances[0].appends[-1]
    assert call["tenant"] == _TENANT and call["action"] == "query.completed"
    for key, value in _fields().items():
        assert call[key] == value, key
    moment = call["ts"]
    assert isinstance(moment, datetime) and moment.tzinfo is not None
    assert moment.utcoffset().total_seconds() == 0, "ts 必须是 UTC"
    assert before <= moment <= datetime.now(timezone.utc)


@pytest.mark.parametrize(
    "error",
    [
        AuditStoreUnavailable("pg_unreachable"),
        ValueError("bad field"),
        RuntimeError("driver exploded"),
        OSError("socket closed"),
    ],
)
def test_append_swallows_chain_failure(monkeypatch, error):
    class _Boom:
        def __init__(self, store) -> None:
            pass

        def append(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(audit_api, "get_audit_store", lambda: object())
    monkeypatch.setattr(audit_api, "AuditChain", _Boom)
    assert audit_api._append(_TENANT, "query.completed", **_fields()) is False


def test_append_returns_false_when_store_unavailable(monkeypatch):
    def _down():
        raise AuditStoreUnavailable("pg_unreachable")

    monkeypatch.setattr(audit_api, "get_audit_store", _down)
    assert audit_api._append(_TENANT, "query.completed", **_fields()) is False
