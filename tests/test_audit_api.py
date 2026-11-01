"""PR-171：审计查询 API（GET /v1/audit）与链A最小生产者（离线契约，不碰真 PG）。

分工：
- 端点层锁租户/角色闸与错误映射（无租户 503、非 admin 403、store 异常 503/400/500）、
  入参归一（limit 夹 500 / offset 负数归 0）、排序口径（run_id -> trace 正序，否则 query 倒序）、
  跨租户（query 参数不能覆盖上下文租户）；
- 生产者层锁「一条事件 + 只落计数/hash + 失败不影响响应」，覆盖 /v1/query 与 /v1/query/stream。
所有 store 一律替身（`hero_quant.audit.api.get_audit_store` 注入），真实 tenant 隔离 /
append-only 由 `tests/test_w4w8_pg.py` 在 PG 守卫下验证。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import pytest
import structlog.contextvars as _cvars
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hero_quant.audit import api as audit_api
from hero_quant.audit.store import (
    AuditError,
    AuditStoreUnavailable,
    AuditValidationError,
)
from hero_quant.auth.context import bind_principal
from hero_quant.auth.middleware import AuthContextMiddleware
from hero_quant.auth.oidc import AuthMode
from hero_quant.auth.principal import Principal, Role

_TENANT = "tenant-a"
_SUBJECT = "user-1"


# ---------------------------------------------------------------------------
# 替身与夹具
# ---------------------------------------------------------------------------
class _FakeAuditStore:
    """AuditStore 替身：记录调用参数，可按需抛错（绝不连 PG）。"""

    def __init__(
        self,
        *,
        rows: list[dict] | None = None,
        query_exc: Exception | None = None,
        trace_exc: Exception | None = None,
        append_ok: bool = True,
        append_exc: Exception | None = None,
    ) -> None:
        self.rows = list(rows or [{"id": 1, "tenant_id": _TENANT}])
        self.query_calls: list[tuple] = []
        self.trace_calls: list[tuple] = []
        self.appends: list[tuple] = []
        self._query_exc = query_exc
        self._trace_exc = trace_exc
        self._append_ok = append_ok
        self._append_exc = append_exc

    def query(self, tenant_id, *, run_id=None, user_id=None, action=None, since=None, until=None, limit=100, offset=0):
        self.query_calls.append(
            {
                "tenant": tenant_id,
                "run_id": run_id,
                "user_id": user_id,
                "action": action,
                "since": since,
                "until": until,
                "limit": limit,
                "offset": offset,
            }
        )
        if self._query_exc is not None:
            raise self._query_exc
        return list(self.rows)

    def trace(self, tenant_id, run_id, *, limit=1000):
        self.trace_calls.append({"tenant": tenant_id, "run_id": run_id, "limit": limit})
        if self._trace_exc is not None:
            raise self._trace_exc
        return list(self.rows)

    def try_append(self, tenant_id, action, **fields):
        self.appends.append({"tenant": tenant_id, "action": action, **fields})
        if self._append_exc is not None:
            raise self._append_exc
        return self._append_ok


def _principal(tenant: str = _TENANT, subject: str = _SUBJECT, *roles: Role) -> Principal:
    return Principal(
        subject=subject,
        tenant_id=tenant,
        roles=frozenset(roles) if roles else frozenset({Role.VIEWER}),
    )


class _StubVerifier:
    """按角色返回 claims；角色走 Principal.from_claims 的真实解析路径。"""

    def __init__(self, *roles: str) -> None:
        self._roles = list(roles)

    def verify(self, token: str) -> dict:
        return {"sub": _SUBJECT, "tenant_id": _TENANT, "roles": self._roles}


def _client(*roles: str) -> TestClient:
    """挂真实 auth 中间件 + audit 路由的 app：验证 contextvar 绑定后的真实判定。"""
    app = FastAPI()
    app.add_middleware(AuthContextMiddleware, verifier=_StubVerifier(*roles), mode=AuthMode.ENFORCE)
    app.include_router(audit_api.router)
    return TestClient(app)


def _server_client() -> TestClient:
    """打真 app 的客户端：**独立 client IP**，不与 fuzz/pr1a 抢 `query:testclient` 限流桶。

    中文：/v1/query 限流键是 `query:{ip}`、窗口 60s/20 次；本文件的端点用例若共用
    默认 IP，会把同轮其它用例（tests/fuzz/test_o_wall_time.py 等）挤成 429 假失败。
    """
    from hero_quant.api import server as srv

    return TestClient(srv.app, client=("audit-probe", 50000))


@pytest.fixture(autouse=True)
def _isolate_contextvars():
    """contextvars 在用例间隔离：避免上个用例绑定的 trace_id/principal 泄漏。"""
    _cvars.clear_contextvars()
    yield
    _cvars.clear_contextvars()


class _FakeChainRecorder:
    """PR-241 后生产者走 AuditChain（入链）：折回 _FakeAuditStore.try_append，断言口径不变。

    中文：本文件只关心「写了几条、字段是什么、失败会不会外抛」。链的 hash/串接由
    tests/test_pr241_audit_chain.py 与 tests/test_pr241_audit_pg.py 负责；这里把
    AuditChain.append 桥到原来的 try_append 记录器，链条失败注入（try_append 抛错）
    因此仍会照原样冒到 _append 的 except 分支。
    """

    def __init__(self, store) -> None:
        self._store = store

    def append(self, tenant, action, **fields):
        fields.pop("ts", None)  # 链层显式注入的时间戳，替身不关心
        if not self._store.try_append(tenant, action, **fields):
            # append_ok=False 表示「存储报失败」：链层没有这种返回值，用异常等价表达。
            raise AuditError("fake_store_reported_failure")
        return {"id": 1, "entry_hash": "e" * 64}


@pytest.fixture(autouse=True)
def _chain_recorder(monkeypatch):
    monkeypatch.setattr(audit_api, "AuditChain", _FakeChainRecorder)


@pytest.fixture
def admin_client():
    return _client(Role.ADMIN.value)


@pytest.fixture
def anon_client():
    """不挂 auth 中间件 = auth 未启用：绝不能因此放行匿名审计查询。"""
    app = FastAPI()
    app.include_router(audit_api.router)
    return TestClient(app)


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeAuditStore()
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    return store


# ---------------------------------------------------------------------------
# 租户 / 角色闸
# ---------------------------------------------------------------------------
def test_audit_route_is_mounted_on_server_without_auth(monkeypatch):
    """server 真的挂上了 /v1/audit（入口存在）；auth 未启用时也必须 503，不许匿名读审计。"""

    def _explode():  # pragma: no cover - 被调用即失败
        raise AssertionError("must not touch audit store without tenant")

    monkeypatch.setattr(audit_api, "get_audit_store", _explode)
    resp = _server_client().get("/v1/audit")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "auth_required_for_audit"


def test_audit_without_tenant_never_falls_back_to_default(anon_client, fake_store):
    """无租户绑定即 503 `auth_required_for_audit`，且绝不拿 "default" 去查（fail-closed）。"""
    resp = anon_client.get("/v1/audit", params={"run_id": "r1"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "auth_required_for_audit"
    assert fake_store.query_calls == [] and fake_store.trace_calls == [], "无租户时不得触达存储"


def test_audit_rejects_non_admin(monkeypatch):
    """analyst/viewer 一律 403（审计是管理面，不是数据面）。"""
    store = _FakeAuditStore()
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    for role in (Role.VIEWER.value, Role.ANALYST.value):
        resp = _client(role).get("/v1/audit", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 403, role
        assert resp.json()["detail"]["reason"] == "audit_requires_admin"
    assert store.query_calls == []


def test_audit_admin_gets_items_and_serialized_ts(admin_client, fake_store):
    ts = datetime(2026, 10, 30, 1, 2, 3, tzinfo=timezone.utc)
    fake_store.rows = [{"id": 7, "tenant_id": _TENANT, "action": "query.completed", "ts": ts}]
    resp = admin_client.get("/v1/audit", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1 and body["limit"] == audit_api.DEFAULT_LIMIT == 100
    assert body["offset"] == 0
    assert body["items"][0]["id"] == 7
    assert body["items"][0]["ts"].startswith("2026-10-30T01:02:03"), "ts 由 FastAPI 直接序列化"
    assert fake_store.query_calls[0]["tenant"] == _TENANT
    assert fake_store.query_calls[0]["run_id"] is None


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------
def test_audit_store_unavailable_maps_to_503(admin_client, monkeypatch):
    store = _FakeAuditStore(query_exc=AuditStoreUnavailable("pg_unreachable"))
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    resp = admin_client.get("/v1/audit", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "audit_store_unavailable"


def test_audit_validation_error_maps_to_400(admin_client, monkeypatch):
    store = _FakeAuditStore(query_exc=AuditValidationError("action_too_long"))
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    resp = admin_client.get("/v1/audit", params={"action": "x" * 300}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "audit_invalid"
    assert resp.json()["detail"]["detail"] == "action_too_long"


def test_audit_generic_error_maps_to_500_without_leaking_reason(admin_client, monkeypatch):
    """非预期审计异常：对外只回通用 code，内部 reason 不进响应体（细节进 structlog）。"""
    store = _FakeAuditStore(query_exc=AuditError("insert_returned_no_row"))
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    resp = admin_client.get("/v1/audit", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 500
    assert resp.json()["detail"]["reason"] == "audit_error"
    assert "insert_returned_no_row" not in resp.text


def test_audit_invalid_since_is_400_not_500(admin_client, fake_store):
    """非法时间格式在落库前就被拒（400），不能变成 PG 报错后的 500。"""
    resp = admin_client.get("/v1/audit", params={"since": "昨天"}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 400
    assert "since_must_be_iso_datetime_or_date" in resp.json()["detail"]["detail"]
    assert fake_store.query_calls == []


# ---------------------------------------------------------------------------
# 入参归一
# ---------------------------------------------------------------------------
def test_audit_limit_clamped_to_hard_cap_and_offset_floored(admin_client, fake_store):
    assert audit_api.MAX_LIMIT == 500, "对外硬上限契约"
    resp = admin_client.get(
        "/v1/audit", params={"limit": 99999, "offset": -7}, headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 200
    assert resp.json()["limit"] == 500
    assert resp.json()["offset"] == 0
    assert fake_store.query_calls[0]["limit"] == 500
    assert fake_store.query_calls[0]["offset"] == 0


# ---------------------------------------------------------------------------
# 排序口径：run_id -> trace（正序），否则 query（倒序）
# ---------------------------------------------------------------------------
def test_audit_run_id_uses_trace_in_ascending_order(admin_client, fake_store):
    fake_store.rows = [{"id": 1, "run_id": "run-1"}, {"id": 2, "run_id": "run-1"}]
    resp = admin_client.get("/v1/audit", params={"run_id": "run-1"}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == [1, 2], "全链路必须时间正序"
    assert fake_store.trace_calls == [{"tenant": _TENANT, "run_id": "run-1", "limit": 100}]
    assert fake_store.query_calls == [], "run_id 模式不得走 query()"


def test_audit_without_run_id_uses_query_desc(admin_client, fake_store):
    fake_store.rows = [{"id": 3}, {"id": 2}, {"id": 1}]
    resp = admin_client.get("/v1/audit", params={"action": "query.completed"}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == [3, 2, 1], "默认时间倒序（store 口径原样透出）"
    assert fake_store.trace_calls == []
    assert fake_store.query_calls[0]["action"] == "query.completed"


def test_audit_tenant_query_param_cannot_override_context(admin_client, fake_store):
    """跨租户：URL 里塞 tenant_id 无效，store 只收到上下文租户。"""
    resp = admin_client.get(
        "/v1/audit",
        params={"tenant_id": "tenant-b", "tenant": "tenant-b", "user_id": _SUBJECT},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    assert fake_store.query_calls[0]["tenant"] == _TENANT
    assert {row["tenant"] for row in fake_store.query_calls} == {_TENANT}


# ---------------------------------------------------------------------------
# 生产者（直接调用）：只落计数/hash，失败不外抛
# ---------------------------------------------------------------------------
def _run(coro_factory):
    return asyncio.run(coro_factory())


def _with_identity():
    """在一个协程里绑定 principal + trace_id（等于 server 中间件 + auth 中间件的效果）。"""
    return bind_principal(_principal(_TENANT, _SUBJECT, Role.ADMIN))


def test_producer_writes_one_event_with_hash_not_plaintext(monkeypatch):
    store = _FakeAuditStore()
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    question = "茅台现在能不能买？结论：买入，目标价 2000。"

    async def _go():
        with _with_identity():
            _cvars.bind_contextvars(trace_id="trace-abc")
            return await audit_api.record_query_audit(
                "query.completed",
                resource="/v1/query",
                model="m-1",
                prompt=question,
                cost={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "cost_usd": 0.0123},
                degraded=["llm_mode_synthetic"],
                iterations=3,
                grounded=True,
            )

    assert _run(_go) is True
    assert len(store.appends) == 1
    row = store.appends[0]
    assert (row["tenant"], row["action"]) == (_TENANT, "query.completed")
    assert row["run_id"] == "trace-abc", "run_id 必须取中间件绑定的 trace_id，不许现编"
    assert row["user_id"] == _SUBJECT
    assert row["resource"] == "/v1/query" and row["model"] == "m-1"
    assert row["prompt_hash"] == hashlib.sha256(question.encode("utf-8")).hexdigest()[:32]
    dumped = json.dumps(row, ensure_ascii=False, default=str)
    assert question not in dumped, "审计绝不能出现 prompt 原文"
    assert "茅台" not in dumped
    assert row["detail"] == {
        "tokens": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        "cost_usd": 0.0123,
        "degraded": ["llm_mode_synthetic"],
        "iterations": 3,
        "grounded": True,
    }


def test_producer_skips_without_tenant(monkeypatch):
    store = _FakeAuditStore()
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    monkeypatch.setattr(audit_api, "current_tenant_id", lambda: None)

    async def _go():
        with _with_identity():
            _cvars.bind_contextvars(trace_id="trace-abc")
            return await audit_api.record_query_audit("query.completed", resource="/v1/query")

    assert _run(_go) is False
    assert store.appends == [], "无租户（auth 未启用）时绝不写审计"


def test_producer_skips_without_trace_id(monkeypatch):
    """trace_id 拿不到就跳过并告警——绝不 uuid 现编，否则与后端日志对不上。"""
    store = _FakeAuditStore()
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)

    async def _go():
        with _with_identity():
            return await audit_api.record_query_audit("query.completed", resource="/v1/query")

    assert _run(_go) is False
    assert store.appends == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"append_ok": False},
        {"append_exc": RuntimeError("pool broken")},
    ],
)
def test_producer_swallows_store_failure(monkeypatch, kwargs):
    store = _FakeAuditStore(**kwargs)
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)

    async def _go():
        with _with_identity():
            _cvars.bind_contextvars(trace_id="trace-abc")
            return await audit_api.record_query_audit("query.failed", resource="/v1/query", error_type="ValueError")

    assert _run(_go) is False, "失败必须如实返回 False，不伪装成功"
    assert len(store.appends) == 1


# ---------------------------------------------------------------------------
# 生产者（端点层）：响应永远不受审计影响
# ---------------------------------------------------------------------------
def test_audit_helper_bounds_wait_on_slow_store(monkeypatch):
    """审计慢（如连接池耗尽等待）必须限时放弃，不能把答案/[DONE] 一起拖住。"""
    import time

    from hero_quant.api import server as srv

    async def _slow(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(audit_api, "record_query_audit", _slow)
    monkeypatch.setattr(srv, "_AUDIT_WRITE_TIMEOUT_S", 0.01)
    started = time.monotonic()
    asyncio.run(srv._audit_query_best_effort("query.completed", resource="/v1/query"))
    assert time.monotonic() - started < 1.0, "审计等待必须有上限"


@pytest.fixture
def audit_probe(monkeypatch):
    """把生产者的身份/存储换替身：请求链路其余部分保持真实（ticket/限流/loop 均打桩）。"""
    store = _FakeAuditStore()
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    monkeypatch.setattr(audit_api, "current_tenant_id", lambda: _TENANT)
    monkeypatch.setattr(
        audit_api, "current_principal", lambda: _principal(_TENANT, _SUBJECT, Role.ANALYST)
    )
    return store


def _stub_loop(monkeypatch, *, error: Exception | None = None):
    from hero_quant.agent.loop import LoopResult
    import hero_quant.agent.loop as loop_mod

    class _StubAgentLoop:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def run(self, q):
            if error is not None:
                raise error
            return LoopResult(
                terminated=True,
                iterations=3,
                text="最终结论",  # 答案正文不得进审计
                reason="complete",
                grounding_verified=True,
            )

    monkeypatch.setattr(loop_mod, "AgentLoop", _StubAgentLoop)


def _ticket():
    from hero_quant.api import security

    return security.issue_ticket(ttl=60)


def test_query_endpoint_writes_single_completed_event(monkeypatch, audit_probe):
    """成功后 try_append 恰好一次；detail 只放计数/标志，prompt 只留 hash。"""

    _stub_loop(monkeypatch)
    question = "600519 现在的估值贵不贵？"
    resp = _server_client().get(
        "/v1/query", params={"q": question}, headers={"X-Ticket": _ticket()}
    )
    assert resp.status_code == 200
    assert len(audit_probe.appends) == 1, "一次查询只写一条审计"
    row = audit_probe.appends[0]
    assert row["action"] == "query.completed"
    assert row["resource"] == "/v1/query"
    assert row["tenant"] == _TENANT
    assert row["user_id"] == _SUBJECT
    assert row["run_id"], "run_id 必须来自中间件绑定的 trace_id"
    assert row["prompt_hash"] == hashlib.sha256(question.encode("utf-8")).hexdigest()[:32]
    dumped = json.dumps(row, ensure_ascii=False, default=str)
    assert question not in dumped and "600519 现在的估值" not in dumped
    assert "最终结论" not in dumped, "答案正文不得进审计"
    assert set(row["detail"]) == {"tokens", "cost_usd", "degraded", "iterations", "grounded"}
    assert row["detail"]["iterations"] == 3
    assert row["detail"]["grounded"] is True


def test_query_endpoint_stays_200_when_audit_raises(monkeypatch, audit_probe):
    """审计写失败（抛错）绝不影响答案与状态码：仍 200 且带正文。"""

    _stub_loop(monkeypatch)
    monkeypatch.setattr(
        audit_api,
        "get_audit_store",
        lambda: _FakeAuditStore(append_exc=RuntimeError("audit pg down")),
    )
    resp = _server_client().get("/v1/query", params={"q": "ping"}, headers={"X-Ticket": _ticket()})
    assert resp.status_code == 200
    assert resp.json()["text"], "答案不能被审计故障吞掉"


def test_query_endpoint_records_failed_event_without_raw_error(monkeypatch, audit_probe):
    """失败路径也留痕，但 detail 只有 error_code/类型名，不带异常原文。"""

    _stub_loop(monkeypatch, error=ValueError("secret prompt fragment leaked"))
    resp = _server_client().get("/v1/query", params={"q": "ping"}, headers={"X-Ticket": _ticket()})
    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"
    assert len(audit_probe.appends) == 1
    row = audit_probe.appends[0]
    assert row["action"] == "query.failed"
    assert row["detail"] == {"error_code": "internal_error", "error_type": "ValueError"}
    assert "secret prompt fragment leaked" not in json.dumps(row, ensure_ascii=False, default=str)


def test_stream_endpoint_writes_single_completed_event(monkeypatch, audit_probe):
    """流式路径：正文推完后写一条 stream.completed，SSE 内容不受影响。"""

    _stub_loop(monkeypatch)
    resp = _server_client().get(
        "/v1/query/stream", params={"q": "ping"}, headers={"X-Ticket": _ticket()}
    )
    assert resp.status_code == 200
    assert "[DONE]" in resp.text
    assert len(audit_probe.appends) == 1
    row = audit_probe.appends[0]
    assert row["action"] == "stream.completed"
    assert row["resource"] == "/v1/query/stream"
    assert row["run_id"]
    assert "ping" != row.get("prompt_hash"), "prompt 只能以 hash 形式落库"
    assert "最终结论" not in json.dumps(row, ensure_ascii=False, default=str)
