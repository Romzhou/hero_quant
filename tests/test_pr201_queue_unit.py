"""PR-201/202/203 纯逻辑用例：假 pool / fake 连接，不连 PG。

真 PG 用例（kill/并发/僵尸/RLS-GUC）在 tests/test_pr201_queue_pg.py，经
"python scripts/run_pg_tests.py tests/test_pr201_queue_pg.py" 真跑。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import pytest

from hero_quant.runs import (
    QueueWorkerOnApiConnection,
    RunQueue,
    RunWorker,
    RunsValidationError,
    StepLedger,
    assert_no_queue_worker,
)
from hero_quant.runs import api as runs_api

_RUN_FIELDS = [
    "id",
    "tenant_id",
    "session_id",
    "status",
    "kind",
    "attempt",
    "max_tries",
    "lease_until",
    "heartbeat_at",
    "payload",
    "result",
    "error",
    "reason",
    "cost",
    "started_at",
    "ended_at",
    "created_at",
]


def _run_row(**overrides: Any) -> tuple:
    base = {
        "id": "run-1",
        "tenant_id": "tenant-a",
        "session_id": None,
        "status": "queued",
        "kind": "agent",
        "attempt": 0,
        "max_tries": 3,
        "lease_until": None,
        "heartbeat_at": None,
        "payload": {},
        "result": None,
        "error": None,
        "reason": None,
        "cost": 0.0,
        "started_at": None,
        "ended_at": None,
        "created_at": None,
    }
    base.update(overrides)
    return tuple(base[name] for name in _RUN_FIELDS)


class _Column:
    def __init__(self, name: str) -> None:
        self.name = name


class _Cursor:
    def __init__(self, rows: list[tuple], columns: list[str]) -> None:
        self._rows = list(rows)
        self.description = [_Column(name) for name in columns]
        self.rowcount = len(self._rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class FakeConn:
    """按 SQL 片段返回预设行；记录每条执行过的语句，供断言 SET/ON CONFLICT 写法。"""

    def __init__(
        self,
        script: list[tuple[str, list[tuple], list[str]]] | None = None,
        guc: str | None = "",
    ) -> None:
        self.script = list(script or [])
        self.executed: list[tuple[str, Any]] = []
        self._guc = guc

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.executed.append((sql, params))
        if "current_setting('app.queue_worker'" in sql:
            return _Cursor([(self._guc,)], ["queue_worker"])
        for marker, rows, columns in self.script:
            if marker in sql:
                return _Cursor(rows, columns)
        return _Cursor([], [])

    @contextmanager
    def transaction(self):
        yield self

    def statements(self, marker: str) -> list[str]:
        return [sql for sql, _ in self.executed if marker in sql]

    def params_for(self, marker: str) -> list[Any]:
        return [params for sql, params in self.executed if marker in sql]


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_assert_no_queue_worker_raises_on_leaked_guc():
    with pytest.raises(QueueWorkerOnApiConnection) as info:
        assert_no_queue_worker(FakeConn(guc="on"))
    assert info.value.error == "queue_worker_guc_leaked"
    assert info.value.reason == "queue_worker_on_api_connection"


def test_assert_no_queue_worker_accepts_empty_and_missing():
    assert_no_queue_worker(FakeConn(guc=""))
    assert_no_queue_worker(FakeConn(guc=None))


def test_runqueue_refuses_connection_with_leaked_guc():
    conn = FakeConn(guc="on")
    queue = RunQueue(FakePool(conn))
    with pytest.raises(QueueWorkerOnApiConnection):
        queue.get("tenant-a", "run-1")
    # 守卫在读任何业务表之前就失败
    assert conn.statements("FROM runs") == []


# ---------------------------------------------------------------------------
# claim：事务级 GUC + 单语句领取
# ---------------------------------------------------------------------------
def test_claim_sets_local_worker_guc_then_reverts_to_tenant():
    conn = FakeConn(
        script=[
            (
                "UPDATE runs SET status = 'running'",
                [_run_row(status="running", attempt=1)],
                _RUN_FIELDS,
            )
        ]
    )
    queue = RunQueue(FakePool(conn))

    run = queue.claim(lease_seconds=60)

    assert run is not None and run["attempt"] == 1
    assert conn.statements("FOR UPDATE SKIP LOCKED"), "领取必须是单语句 SKIP LOCKED"
    # 批次15：跨租户靠数据库角色策略，领取事务不再开 worker GUC（旧旁路已删除）
    assert not conn.statements("set_config('app.queue_worker'"), "不得再依赖 worker GUC 开关"
    # 领取成功后同一事务内立刻切租户
    assert conn.statements("set_config('app.tenant_id'")
    # 绝不允许会话级 SET（会在连接回池后残留）
    assert not conn.statements("SET app"), "禁止会话级 SET"
    assert queue.claim_registry_size() == 1


def test_claim_returns_none_when_queue_empty():
    queue = RunQueue(FakePool(FakeConn()))
    assert queue.claim() is None
    assert queue.claim_registry_size() == 0


def test_claim_rejects_bad_lease():
    queue = RunQueue(FakePool(FakeConn()))
    with pytest.raises(RunsValidationError):
        queue.claim(lease_seconds=0)


# ---------------------------------------------------------------------------
# fencing
# ---------------------------------------------------------------------------
def test_finish_returns_false_and_logs_when_fenced(caplog):
    conn = FakeConn()  # UPDATE ... RETURNING 无行 = 影响 0 行
    queue = RunQueue(FakePool(conn))
    queue._remember_claim("run-1", "tenant-a", 1)

    with caplog.at_level(logging.WARNING, logger="hero_quant.runs"):
        assert queue.finish("run-1", 1, result={"ok": True}) is False

    assert "attempt = %s" in conn.statements("UPDATE runs SET status = 'succeeded'")[0]
    assert "runs.write_fenced" in caplog.text


def test_finish_passes_attempt_and_dispatches_tenant_guc():
    conn = FakeConn(
        script=[("UPDATE runs SET status = 'succeeded'", [("run-1",)], ["id"])]
    )
    queue = RunQueue(FakePool(conn))
    queue._remember_claim("run-1", "tenant-a", 1)

    assert queue.finish("run-1", 1, result={"ok": True}) is True
    params = conn.params_for("UPDATE runs SET status = 'succeeded'")[0]
    assert params[1:] == ("run-1", 1)
    assert conn.params_for("set_config('app.tenant'")[0] == ("tenant-a",)
    assert queue.claim_registry_size() == 0, "收尾后必须清台账（避免无界增长）"


def test_finish_without_claim_discards_instead_of_writing(caplog):
    conn = FakeConn()
    queue = RunQueue(FakePool(conn))
    with caplog.at_level(logging.WARNING, logger="hero_quant.runs"):
        assert queue.finish("run-1", 1) is False
    assert conn.statements("UPDATE runs SET status = 'succeeded'") == []
    assert "runs.write_fenced" in caplog.text


def test_fail_requeues_until_max_tries_then_dead():
    requeue = FakeConn(
        script=[("UPDATE runs SET", [("queued", 1, 3)], ["status", "attempt", "max_tries"])]
    )
    queue = RunQueue(FakePool(requeue))
    queue._remember_claim("run-1", "tenant-a", 1)
    outcome = queue.fail("run-1", 1, error="boom")
    assert outcome["requeued"] is True and outcome["dead"] is False
    assert "attempt >= max_tries" in requeue.statements("UPDATE runs SET")[0]

    dead = FakeConn(
        script=[("UPDATE runs SET", [("dead", 1, 1)], ["status", "attempt", "max_tries"])]
    )
    queue2 = RunQueue(FakePool(dead))
    queue2._remember_claim("run-1", "tenant-a", 1)
    outcome2 = queue2.fail("run-1", 1, error="boom")
    assert outcome2["dead"] is True and outcome2["requeued"] is False


def test_fail_fenced_returns_fenced_marker(caplog):
    queue = RunQueue(FakePool(FakeConn()))
    queue._remember_claim("run-1", "tenant-a", 1)
    with caplog.at_level(logging.WARNING, logger="hero_quant.runs"):
        outcome = queue.fail("run-1", 1, error="boom")
    assert outcome["status"] == "fenced"
    assert "runs.write_fenced" in caplog.text


# ---------------------------------------------------------------------------
# enqueue 入参
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "unknown"},
        {"payload": ["not", "a", "mapping"]},
        {"max_tries": 0},
        {"max_tries": 1000},
    ],
)
def test_enqueue_rejects_invalid_arguments(overrides):
    queue = RunQueue(FakePool(FakeConn()))
    params = {"tenant_id": "tenant-a", "kind": "agent", "payload": {}}
    params.update(overrides)
    with pytest.raises(RunsValidationError):
        queue.enqueue(**params)


# ---------------------------------------------------------------------------
# StepLedger
# ---------------------------------------------------------------------------
def test_step_ledger_requires_tenant_context():
    ledger = StepLedger(FakePool(FakeConn()))
    with pytest.raises(RunsValidationError):
        ledger.done_steps("run-1")


def test_step_record_uses_conflict_do_nothing():
    conn = FakeConn(script=[("INSERT INTO run_steps", [("fetch",)], ["step_name"])])
    ledger = StepLedger(FakePool(conn), tenant_id="tenant-a")
    assert ledger.record("run-1", "tenant-a", "fetch", output={"n": 1}) is True
    sql = conn.statements("INSERT INTO run_steps")[0]
    assert "ON CONFLICT (run_id, step_name) DO NOTHING" in sql

    dup = FakeConn()  # 冲突时 RETURNING 无行
    ledger_dup = StepLedger(FakePool(dup), tenant_id="tenant-a")
    assert ledger_dup.record("run-1", "tenant-a", "fetch") is False


# ---------------------------------------------------------------------------
# RunWorker（假 queue）
# ---------------------------------------------------------------------------
class FakeQueue:
    def __init__(self, pool: FakePool, *, status: str = "running", run: dict | None = None) -> None:
        self.pool = pool
        self.sent_status = status
        self.run = run or {"id": "run-1", "tenant_id": "tenant-a", "attempt": 1, "payload": {}}
        self.finished: list[tuple] = []
        self.failed: list[tuple] = []
        self.settled: list[tuple] = []
        self.forgotten: list[tuple] = []

    def claim(self, *, lease_seconds: int = 300):
        return self.run

    def finish(self, run_id: str, attempt: int, *, result: Any = None) -> bool:
        self.finished.append((run_id, attempt, result))
        return True

    def fail(self, run_id: str, attempt: int, *, error: str) -> dict:
        self.failed.append((run_id, attempt, error))
        return {"status": "queued", "requeued": True, "dead": False}

    def heartbeat(self, run_id: str, attempt: int, *, lease_seconds: int = 300) -> bool:
        return True

    def settle_cancel(self, tenant_id: str, run_id: str, attempt: int) -> bool:
        self.settled.append((tenant_id, run_id, attempt))
        return True

    def _status(self, tenant_id: str, run_id: str) -> str:
        return self.sent_status

    def _forget(self, run_id: str, attempt: int | None = None) -> None:
        self.forgotten.append((run_id, attempt))


def _worker(queue: FakeQueue, handler) -> RunWorker:
    return RunWorker(queue, handler, lease_seconds=30, heartbeat_interval=3600, poll_interval=0.01)


def test_worker_skips_completed_steps_on_resume():
    conn = FakeConn(
        script=[
            ("SELECT step_name, output FROM run_steps", [("fetch", {"rows": 3})],
             ["step_name", "output"])
        ]
    )
    queue = FakeQueue(FakePool(conn))
    seen: list[bool] = []

    def handler(run, ctx):
        with ctx.step("fetch") as step:
            seen.append(step.skipped)
            assert step.output == {"rows": 3}
        with ctx.step("score") as step:
            step.output = {"score": 1}
        return {"ok": True}

    assert _worker(queue, handler).run_once() is True
    assert seen == [True], "已完成 step 必须跳过，不能重跑"
    assert queue.finished[0][2] == {"ok": True}
    assert [params[2] for params in conn.params_for("INSERT INTO run_steps")] == ["score"]


def test_worker_fails_run_without_writing_failed_step(caplog):
    conn = FakeConn()
    queue = FakeQueue(FakePool(conn))

    def handler(run, ctx):
        with ctx.step("fetch"):
            raise RuntimeError("vendor down")
        return None

    with caplog.at_level(logging.WARNING, logger="hero_quant.runs"):
        _worker(queue, handler).run_once()

    assert queue.finished == []
    assert queue.failed and queue.failed[0][2].endswith("vendor down")
    # run_steps 只记「已完成」：失败写行会让重跑成功无法落盘（ON CONFLICT DO NOTHING）
    assert conn.params_for("INSERT INTO run_steps") == []
    assert "runs.step_failed" in caplog.text and "step=fetch" in caplog.text


def test_worker_stops_at_step_boundary_when_cancelling():
    conn = FakeConn()
    queue = FakeQueue(FakePool(conn), status="cancelling")
    ran: list[bool] = []

    def handler(run, ctx):
        ran.append("handler")
        with ctx.step("fetch"):
            ran.append("step")
        return None

    _worker(queue, handler).run_once()

    # handler 已经开始执行（worker 不打断任意代码），但第一个 step 边界就抛取消，
    # step 内函数体不得执行 —— 这就是「取消在 step 边界生效」的口径。
    assert ran == ["handler"], "取消在 step 边界生效：step 内函数体不应执行"
    assert queue.settled == [("tenant-a", "run-1", 1)]
    assert queue.finished == []


# ---------------------------------------------------------------------------
# API（假 queue）
# ---------------------------------------------------------------------------
class FakeApiQueue:
    def __init__(self, pool: FakePool, *, status: str = "queued") -> None:
        self.pool = pool
        self.status = status
        self.cancelled: list[str] = []
        self.enqueued: list[tuple] = []

    def enqueue(self, tenant_id, kind, payload, *, session_id=None, max_tries=3, run_id=None):
        self.enqueued.append((tenant_id, kind, dict(payload)))
        return {"id": "run-1", "tenant_id": tenant_id, "status": "queued", "kind": kind}

    def list(self, tenant_id, *, status=None, limit=50):
        return [{"id": "run-1", "tenant_id": tenant_id, "status": self.status}]

    def get(self, tenant_id, run_id):
        return {
            "id": run_id,
            "tenant_id": tenant_id,
            "status": self.status,
            "payload": {},
            "attempt": 1,
        }

    def request_cancel(self, tenant_id, run_id):
        self.cancelled.append(run_id)
        return {
            "id": run_id,
            "tenant_id": tenant_id,
            "status": "cancelling",
            "cancelled": True,
        }


@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake = FakeApiQueue(FakePool(FakeConn()))
    monkeypatch.setattr(runs_api, "require_tenant", lambda: "tenant-a")
    monkeypatch.setattr(runs_api, "_EVENT_POLL_SECONDS", 0.0)
    # keep-alive 是 15s 一条；压到 0 才能在用例内观察到（不 sleep 真 15s）
    monkeypatch.setattr(runs_api, "_KEEP_ALIVE_SECONDS", 0.0)
    runs_api.set_run_queue(fake)
    app = FastAPI()
    app.include_router(runs_api.router, prefix="/v1/runs")
    try:
        yield TestClient(app), fake
    finally:
        runs_api.reset_run_queue()


def test_api_enqueue_returns_queued_run(client):
    http, fake = client
    response = http.post("/v1/runs", json={"kind": "agent", "payload": {"q": "x"}})
    assert response.status_code == 201
    assert response.json()["run"]["status"] == "queued"
    assert fake.enqueued == [("tenant-a", "agent", {"q": "x"})]


def test_api_list_and_detail(client):
    http, _ = client
    assert http.get("/v1/runs").json()["count"] == 1
    detail = http.get("/v1/runs/run-1").json()
    assert detail["run"]["id"] == "run-1"
    assert detail["progress"] == {"done_steps": 0, "total_steps": None}


def test_api_cancel_returns_cancelled_flag(client):
    http, fake = client
    payload = http.post("/v1/runs/run-1/cancel").json()
    assert payload["cancelled"] is True
    assert payload["run"]["status"] == "cancelling"
    assert fake.cancelled == ["run-1"]


def test_api_maps_not_found_to_404(client, monkeypatch):
    http, fake = client

    def missing(tenant_id, run_id):
        from hero_quant.runs import RunNotFound

        raise RunNotFound("unknown run")

    monkeypatch.setattr(fake, "get", missing)
    response = http.get("/v1/runs/missing")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "run_not_found"


def test_api_surfaces_guc_leak_as_503(client, monkeypatch):
    http, fake = client

    def leak(*_args, **_kwargs):
        raise QueueWorkerOnApiConnection("app.queue_worker='on'")

    monkeypatch.setattr(fake, "list", leak)
    response = http.get("/v1/runs")
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "queue_worker_guc_leaked"


def test_api_sse_emits_lifecycle_events(client, monkeypatch):
    http, fake = client
    initial = {
        "id": "run-1",
        "tenant_id": "tenant-a",
        "status": "running",
        "payload": {},
        "attempt": 1,
    }
    final = {
        "id": "run-1",
        "tenant_id": "tenant-a",
        "status": "succeeded",
        "payload": {},
        "attempt": 1,
    }
    states = iter([initial, final, final])
    monkeypatch.setattr(fake, "get", lambda tenant_id, run_id: next(states, final))
    body = http.get("/v1/runs/run-1/events").text
    assert "event: run.running" in body
    assert "event: run.progress" in body
    assert "event: run.done" in body
    assert ": keep-alive" in body
