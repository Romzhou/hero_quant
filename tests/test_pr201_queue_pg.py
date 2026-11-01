"""PR-201/202/203 真 PG 验收：kill 续跑 / 3 worker 并发 / 僵尸 fencing / RLS-GUC。

跑法（唯一入口，避免手拼 DSN）：
    python scripts/run_pg_tests.py tests/test_pr201_queue_pg.py

隔离：每个用例一个临时 schema（apply_migrations 建表）+ 用完 DROP CASCADE，不污染共享库。
RLS 用例需要非 superuser 角色（superuser 走 BYPASSRLS，策略对它不生效）：优先用
HERO_RLS_TEST_DSN，否则从 pg_roles 里挑一个 NOSUPERUSER 登录角色并 SET ROLE；都没有则
相关用例 skip 并给原因（不静默绿灯）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
pytestmark = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "")

psycopg = pytest.importorskip("psycopg")

from hero_quant.db.runner import apply_migrations  # noqa: E402
from hero_quant.runs import RunQueue, RunWorker, StepLedger  # noqa: E402
from hero_quant.runs.guards import (  # noqa: E402
    QueueWorkerOnApiConnection,
    assert_no_queue_worker,
)

_TENANT_A = "tenant-w3-a"
_TENANT_B = "tenant-w3-b"


def _dsn() -> str:
    return pg_test_dsn("w3_runs_queue_pg").replace("postgresql+psycopg://", "postgresql://", 1)


def _wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.05, message: str = "timeout"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# 连接池夹具
# ---------------------------------------------------------------------------
class _Pool:
    """每次 checkout 新连接（线程安全）；可选 SET ROLE 到非 superuser 角色。"""

    def __init__(self, dsn: str, schema: str, *, role: str | None = None) -> None:
        self._dsn = dsn
        self._schema = schema
        self._role = role

    @contextmanager
    def connection(self):
        conn = psycopg.connect(self._dsn, autocommit=False)
        try:
            conn.execute('SET search_path TO "%s"' % self._schema)
            if self._role:
                conn.execute('SET ROLE "%s"' % self._role)
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()


class _SinglePool:
    """复用同一条连接（验证 SET LOCAL 随事务失效）；非线程安全，只给单线程用例。"""

    def __init__(self, dsn: str, schema: str, *, role: str | None = None) -> None:
        self._conn = psycopg.connect(dsn, autocommit=False)
        self._conn.execute('SET search_path TO "%s"' % schema)
        if role:
            self._conn.execute('SET ROLE "%s"' % role)

    @contextmanager
    def connection(self):
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def scalar(self, sql: str, params=None):
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        try:
            self._conn.rollback()
        finally:
            self._conn.close()


def _find_rls_role(admin) -> str | None:
    """挑一个能真跑 RLS 的登录角色（非 superuser、无 BYPASSRLS）。"""
    row = admin.execute(
        "SELECT rolname FROM pg_roles"
        " WHERE NOT rolsuper AND NOT rolbypassrls AND rolcanlogin"
        " AND rolname <> current_user ORDER BY rolname LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


@pytest.fixture()
def schema():
    name = "w3_queue_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(_dsn(), autocommit=True)
    try:
        admin.execute('CREATE SCHEMA "%s"' % name)
    except psycopg.Error as exc:  # pragma: no cover - 权限不足时给明确 skip
        admin.close()
        pytest.skip("cannot create temp schema for isolation: %s" % type(exc).__name__)
    role = None
    try:
        admin.execute('SET search_path TO "%s"' % name)
        apply_migrations(admin)
        role = _find_rls_role(admin)
        if role is not None:
            admin.execute('GRANT USAGE ON SCHEMA "%s" TO "%s"' % (name, role))
            admin.execute(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "%s" TO "%s"'
                % (name, role)
            )
        yield name, role
    finally:
        admin.execute('DROP SCHEMA "%s" CASCADE' % name)
        admin.close()


@pytest.fixture()
def queue(schema):
    name, _role = schema
    return RunQueue(_Pool(_dsn(), name))


@pytest.fixture()
def admin(schema):
    name, _role = schema
    conn = psycopg.connect(_dsn(), autocommit=True)
    conn.execute('SET search_path TO "%s"' % name)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def rls(schema):
    name, role = schema
    if role is None:
        pytest.skip("无可用非 superuser 登录角色（BYPASSRLS 无法证明 RLS），RLS 用例跳过")
    pool = _SinglePool(_dsn(), name, role=role)
    try:
        yield pool, role, name
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# 基础闭环
# ---------------------------------------------------------------------------
def test_enqueue_claim_steps_finish_roundtrip(queue):
    run = queue.enqueue(_TENANT_A, "agent", {"total_steps": 2})
    assert run["status"] == "queued" and run["attempt"] == 0
    assert run["payload"] == {"total_steps": 2}

    claimed = queue.claim(lease_seconds=60)
    assert claimed is not None
    assert (claimed["id"], claimed["attempt"], claimed["status"]) == (run["id"], 1, "running")
    assert queue.claim() is None

    assert queue.heartbeat(run["id"], 1, lease_seconds=60) is True

    ledger = StepLedger(queue.pool, tenant_id=_TENANT_A)
    assert ledger.record(run["id"], _TENANT_A, "fetch", output={"rows": 3}, attempt=1) is True
    assert ledger.record(run["id"], _TENANT_A, "fetch", output={"rows": 4}, attempt=1) is False
    assert ledger.done_steps(run["id"]) == {"fetch"}
    assert ledger._done_outputs(run["id"]) == {"fetch": {"rows": 3}}

    assert queue.finish(run["id"], 1, result={"ok": True}) is True
    row = queue.get(_TENANT_A, run["id"])
    assert row["status"] == "succeeded"
    assert row["result"] == {"ok": True}
    assert row["ended_at"] is not None

    with pytest.raises(Exception):
        queue.get(_TENANT_B, run["id"])


def test_cancel_queued_run_is_terminal(queue):
    run = queue.enqueue(_TENANT_A, "backtest", {})
    out = queue.request_cancel(_TENANT_A, run["id"])
    assert out["status"] == "cancelled" and out["cancelled"] is True
    again = queue.request_cancel(_TENANT_A, run["id"])
    assert again["cancelled"] is False and again["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 硬门 1：僵尸 worker 的写入被 fencing 拒绝
# ---------------------------------------------------------------------------
def test_stale_worker_write_is_fenced(schema):
    name, _role = schema
    queue = RunQueue(_Pool(_dsn(), name))
    admin = psycopg.connect(_dsn(), autocommit=True)
    admin.execute('SET search_path TO "%s"' % name)
    try:
        _assert_stale_write_fenced(queue, admin)
    finally:
        admin.close()


def _assert_stale_write_fenced(queue, admin):
    run = queue.enqueue(_TENANT_A, "agent", {})
    stale = RunQueue(_Pool(_dsn(), _schema_of(queue)))
    first = stale.claim(lease_seconds=60)
    assert first is not None and first["attempt"] == 1

    # 让租约过期但不杀 worker：等价于进程失联（CPU 卡死 / 网络分区）
    admin.execute(
        "UPDATE runs SET lease_until = now() - interval '1 minute' WHERE id = %s",
        (run["id"],),
    )
    assert queue.reap_expired() == {"requeued": 1, "dead": 0}

    revived = RunQueue(_Pool(_dsn(), _schema_of(queue)))
    second = revived.claim(lease_seconds=60)
    assert second is not None and second["attempt"] == 2 and second["id"] == run["id"]

    # 僵尸 worker 拿着旧 attempt 写结果：必须 0 行、返回 False
    assert stale.finish(run["id"], 1, result={"stale": True}) is False
    assert stale.fail(run["id"], 1, error="stale")["status"] == "fenced"
    assert stale.heartbeat(run["id"], 1, lease_seconds=60) is False

    row = revived.get(_TENANT_A, run["id"])
    assert row["status"] == "running" and row["attempt"] == 2
    assert row["result"] is None, "僵尸 worker 的结果绝不能落库"


def test_reap_marks_dead_after_max_tries(queue, admin):
    run = queue.enqueue(_TENANT_A, "research", {}, max_tries=1)
    claimed = queue.claim(lease_seconds=60)
    assert claimed is not None and claimed["attempt"] == 1

    admin.execute(
        "UPDATE runs SET lease_until = now() - interval '1 minute' WHERE id = %s",
        (run["id"],),
    )
    assert queue.reap_expired() == {"requeued": 0, "dead": 1}
    assert queue.get(_TENANT_A, run["id"])["status"] == "dead"


# ---------------------------------------------------------------------------
# 硬门 2：3 worker 抢 10 个任务，每个恰好成功执行一次
# ---------------------------------------------------------------------------
def test_three_workers_each_of_ten_jobs_runs_exactly_once(schema):
    name, _role = schema
    queue = RunQueue(_Pool(_dsn(), name))
    run_ids = [queue.enqueue("tenant-w3-conc", "agent", {"i": i})["id"] for i in range(10)]

    counts = {run_id: 0 for run_id in run_ids}
    lock = threading.Lock()
    stop = threading.Event()
    errors: list[BaseException] = []

    def make_handler():
        def handler(run, ctx):
            with ctx.step("work") as step:
                if not step.skipped:
                    with lock:
                        counts[run["id"]] += 1
                step.output = {"i": run["payload"]["i"]}
            time.sleep(0.05)
            return {"done": run["id"]}

        return handler

    workers = [
        RunWorker(
            RunQueue(_Pool(_dsn(), name)),
            make_handler(),
            lease_seconds=60,
            heartbeat_interval=3600,
            poll_interval=0.02,
        )
        for _ in range(3)
    ]

    def loop(worker):
        while not stop.is_set():
            try:
                if worker.run_once():
                    continue
            except BaseException as exc:  # noqa: BLE001 - 线程内异常必须回传给主线程
                errors.append(exc)
                return
            stop.wait(0.02)

    threads = [threading.Thread(target=loop, args=(worker,), daemon=True) for worker in workers]
    for thread in threads:
        thread.start()
    try:
        def all_done():
            rows = queue.list("tenant-w3-conc", limit=20)
            return len(rows) == 10 and all(row["status"] == "succeeded" for row in rows)

        _wait_for(all_done, timeout=60, message="10 个任务未在超时内全部成功")
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)

    assert errors == [], f"worker 线程异常: {errors!r}"
    assert all(count == 1 for count in counts.values()), f"存在重复执行: {counts}"
    assert set(counts) == set(run_ids)
    assert all(worker.queue.claim_registry_size() == 0 for worker in workers)


# ---------------------------------------------------------------------------
# 硬门 3：SIGKILL 后续跑，已完成 step 不重跑
# ---------------------------------------------------------------------------
_WORKER_SCRIPT = textwrap.dedent(
    """
    import os, pathlib, time
    from hero_quant.runs import RunQueue, RunWorker

    dsn = os.environ["W3_QUEUE_DSN"]
    marker_dir = pathlib.Path(os.environ["W3_QUEUE_MARKERS"])
    run_id = os.environ["W3_QUEUE_RUN"]
    queue = RunQueue.from_dsn(dsn)

    def mark(name):
        (marker_dir / (run_id + "." + name + "." + str(os.getpid()))).write_text("1", encoding="utf-8")

    def handler(run, ctx):
        steps = []
        for name, delay in (("s1", 0.0), ("s2", 0.6), ("s3", 0.0), ("s4", 0.0)):
            with ctx.step(name) as step:
                if step.skipped:
                    steps.append({"step": name, "skipped": True, "value": step.output})
                    continue
                mark(name)
                time.sleep(delay)
                step.output = {"step": name, "value": name.upper()}
                steps.append({"step": name, "skipped": False, "value": step.output})
        return {"steps": steps, "pid": os.getpid()}

    worker = RunWorker(queue, handler, lease_seconds=2, heartbeat_interval=0.5, poll_interval=0.05)
    while True:
        worker.run_once()
    """
)


def _worker_env(name: str, marker_dir, run_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env["W3_QUEUE_DSN"] = _dsn()
    env["W3_QUEUE_MARKERS"] = str(marker_dir)
    env["W3_QUEUE_RUN"] = run_id
    env["PGOPTIONS"] = "-c search_path=%s" % name
    return env


def test_sigkill_then_resume_from_step_checkpoint(schema, queue, tmp_path):
    name, _role = schema
    script = tmp_path / "queue_worker.py"
    script.write_text(_WORKER_SCRIPT, encoding="utf-8")
    markers = tmp_path / "markers"
    markers.mkdir()

    run = queue.enqueue(_TENANT_A, "agent", {"total_steps": 4}, max_tries=3)
    env = _worker_env(name, markers, run["id"])

    first = subprocess.Popen(
        [sys.executable, str(script)], env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait_for(
            lambda: list(markers.glob(run["id"] + ".s2.*")),
            timeout=30, message="第一个 worker 未跑到 s2（未能验证 kill 续跑）",
        )
        first.send_signal(signal.SIGKILL)
        assert first.wait(timeout=15) == -signal.SIGKILL
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)
    first_pid = first.pid

    # 租约 2s：等它过期再走看门狗，模拟真实运维路径（而不是直接改库快进）
    time.sleep(2.5)
    assert queue.reap_expired()["requeued"] == 1

    second = subprocess.Popen(
        [sys.executable, str(script)], env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        def finished():
            row = queue.get(_TENANT_A, run["id"])
            return row if row["status"] in {"succeeded", "failed", "dead"} else None

        row = _wait_for(finished, timeout=60, message="重启后的 worker 未完成续跑")
        assert row["status"] == "succeeded", row.get("error")
        result = row["result"]
        assert [step["step"] for step in result["steps"]] == ["s1", "s2", "s3", "s4"]
        assert result["pid"] != first_pid
        # s1 中断点已记录 → 续跑必须跳过且能拿回旧输出
        assert result["steps"][0]["skipped"] is True
        assert result["steps"][0]["value"] == {"step": "s1", "value": "S1"}
        assert result["steps"][1]["skipped"] is False
    finally:
        if second.poll() is None:
            second.kill()
            second.wait(timeout=10)

    # s1 只执行一次；s2 被 kill 打断（未落 step），续跑重做一次 = 2 次
    assert len(list(markers.glob(run["id"] + ".s1.*"))) == 1
    assert len(list(markers.glob(run["id"] + ".s2.*"))) == 2
    assert len(list(markers.glob(run["id"] + ".s3.*"))) == 1
    assert len(StepLedger(queue.pool, tenant_id=_TENANT_A).done_steps(run["id"])) == 4


# ---------------------------------------------------------------------------
# 取消：step 边界 5s 内生效
# ---------------------------------------------------------------------------
def test_cancel_takes_effect_at_next_step_boundary(queue):
    run = queue.enqueue(_TENANT_A, "agent", {})
    started = threading.Event()

    def handler(run_row, ctx):
        for index in range(1, 6):
            with ctx.step("s%d" % index):
                started.set()
                time.sleep(0.3)
        return {"finished": True}

    worker = RunWorker(queue, handler, lease_seconds=60, heartbeat_interval=3600, poll_interval=0.05)
    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    try:
        ledger = StepLedger(queue.pool, tenant_id=_TENANT_A)
        _wait_for(lambda: "s1" in ledger.done_steps(run["id"]), timeout=20,
                  message="handler 未开始执行")
        assert queue.request_cancel(_TENANT_A, run["id"])["status"] == "cancelling"
        cancelled_at = time.monotonic()
        thread.join(timeout=10)
        assert not thread.is_alive(), "取消未在 step 边界生效（线程仍阻塞）"
        elapsed = time.monotonic() - cancelled_at
    finally:
        if thread.is_alive():
            thread.join(timeout=1)

    row = queue.get(_TENANT_A, run["id"])
    assert row["status"] == "cancelled"
    assert row["result"] is None
    assert elapsed < 5.0, "取消应在 step 边界 5s 内生效，实测 %.2fs" % elapsed
    assert "s5" not in StepLedger(queue.pool, tenant_id=_TENANT_A).done_steps(run["id"])


# ---------------------------------------------------------------------------
# RLS/GUC 三条附加验收
# ---------------------------------------------------------------------------
def test_claim_uses_set_local_and_leaves_no_worker_guc(schema, rls):
    pool, _role, _name = rls
    name, _role2 = schema
    admin_queue = RunQueue(_Pool(_dsn(), name))
    admin_queue.enqueue(_TENANT_A, "agent", {})
    admin_queue.enqueue(_TENANT_B, "agent", {})

    queue = RunQueue(pool)
    claimed = queue.claim(lease_seconds=60)
    assert claimed is not None and claimed["tenant_id"] in {_TENANT_A, _TENANT_B}

    # ① 同一连接、事务外：SET LOCAL 已随提交失效，绝不能残留 'on'
    leaked = pool.scalar("SELECT current_setting('app.queue_worker', true)")
    assert (leaked or "").strip() == "", "SET LOCAL 提交后不应残留 worker GUC，实际 %r" % (leaked,)

    # 跨租户可见性同时收回：没有租户上下文时 0 行（不是 2 行）
    assert pool.scalar("SELECT count(*) FROM runs") == 0


def test_worker_guc_visible_only_inside_claim_transaction(schema):
    name, role = schema
    if role is None:
        pytest.skip("无可用非 superuser 登录角色（BYPASSRLS 无法证明 RLS），RLS 用例跳过")
    pool = _SinglePool(_dsn(), name, role=role)
    try:
        admin_queue = RunQueue(_Pool(_dsn(), name))
        admin_queue.enqueue(_TENANT_A, "agent", {})
        admin_queue.enqueue(_TENANT_B, "agent", {})

        with pool.connection() as conn:
            assert (conn.execute("SELECT current_setting('app.queue_worker', true)").fetchone()[0] or "") == ""
            conn.execute("SELECT set_config('app.queue_worker', 'on', true)")
            visible = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        assert visible == 2, "领取事务内 worker GUC 应看到全部租户，实际 %s 行" % visible

        cleared = pool.scalar(
            "SELECT COALESCE(current_setting('app.queue_worker', true), '') = ''"
        )
        assert cleared is True
        assert pool.scalar("SELECT count(*) FROM runs") == 0
    finally:
        pool.close()


def test_api_guard_rejects_leaked_worker_guc(schema):
    name, role = schema
    if role is None:
        pytest.skip("无可用非 superuser 登录角色（BYPASSRLS 无法证明 RLS），RLS 用例跳过")
    conn = psycopg.connect(_dsn(), autocommit=False)
    try:
        conn.execute('SET search_path TO "%s"' % name)
        conn.execute('SET ROLE "%s"' % role)
        conn.execute("SET app.queue_worker = 'on'")  # 会话级：模拟泄漏（正是要禁止的写法）
        conn.commit()

        with pytest.raises(QueueWorkerOnApiConnection):
            assert_no_queue_worker(conn)

        class _LeakyPool:
            @contextmanager
            def connection(self):
                yield conn

        with pytest.raises(QueueWorkerOnApiConnection):
            RunQueue(_LeakyPool()).list(_TENANT_A)
    finally:
        try:
            conn.execute("RESET app.queue_worker")
            conn.commit()
        except psycopg.Error:
            conn.rollback()
        conn.close()


def _schema_of(queue: RunQueue) -> str:
    """测试专用：从测试池里取回临时 schema 名（避免把 schema 名传遍每个断言）。"""
    pool = queue.pool
    return pool._schema
