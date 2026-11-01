"""PR-241 真 PG：链式哈希追加/校验、历史行覆盖口径、篡改检测、跨租户隔离、并发不分叉。

门控与隔离同 tests/test_data_snapshot_pg.py：PG 守卫不可用则整模块 skip；临时 schema
跑完 DROP CASCADE，不污染共享库。运行方式（不要在并发全量里跑）：

    python scripts/run_pg_tests.py tests/test_pr241_audit_pg.py

篡改用例需要临时 DISABLE 触发器（audit_log 的 append-only 由 0003 的触发器保证），
这正是「应用代码绕不过去」的证据：非 owner 角色拿不到 → skip，不假装通过。
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
pytestmark = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "")

psycopg = pytest.importorskip("psycopg")

from hero_quant.audit import api as audit_api  # noqa: E402
from hero_quant.audit import retention as retention_mod  # noqa: E402
from hero_quant.audit.chain import GENESIS_HASH, AuditChain  # noqa: E402
from hero_quant.audit.retention import retention_plan  # noqa: E402
from hero_quant.audit.store import AuditStore  # noqa: E402
from hero_quant.db.runner import apply_migrations  # noqa: E402


def _conninfo() -> str:
    return pg_test_dsn("w2_audit_pg").replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture()
def schema():
    name = f"w2_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(_conninfo(), autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(f"cannot create temp schema for isolation: {type(exc).__name__}")
    try:
        yield name
    finally:
        admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        admin.close()


@pytest.fixture()
def conn(schema):
    connection = psycopg.connect(_conninfo(), autocommit=True)
    connection.execute(f'SET search_path TO "{schema}"')
    try:
        yield connection
    finally:
        connection.close()


class _Pool:
    """每次 connection() 新建连接：与 psycopg_pool 的上下文形态一致，且线程安全。"""

    def __init__(self, schema: str) -> None:
        self._schema = schema
        self._dsn = _conninfo()

    @contextmanager
    def connection(self):
        connection = psycopg.connect(self._dsn, autocommit=True)
        connection.execute(f'SET search_path TO "{self._schema}"')
        try:
            yield connection
        finally:
            connection.close()


@pytest.fixture()
def store(conn, schema):
    apply_migrations(conn)
    return AuditStore(_Pool(schema))


def _disable_append_only(conn) -> None:
    """临时关掉 append-only 触发器以模拟「已经进库的篡改」；拿不到权限就 skip。"""
    try:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only_trigger")
    except psycopg.Error as exc:
        pytest.skip(f"cannot disable append-only trigger: {type(exc).__name__}")


def _enable_append_only(conn) -> None:
    conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only_trigger")


def _raw_rows(conn):
    return conn.execute(
        "SELECT id, prev_hash, entry_hash, detail FROM audit_log ORDER BY id ASC"
    ).fetchall()


# ---------------------------------------------------------------------------
# 追加 / 链头 / 校验
# ---------------------------------------------------------------------------
def test_append_builds_linked_chain(store, conn):
    chain = AuditChain(store)
    first = chain.append("tenant-a", "tool.call", resource="quote", detail={"ok": True})
    second = chain.append("tenant-a", "backtest.run", run_id="run-1")

    assert first["prev_hash"] == GENESIS_HASH
    assert second["prev_hash"] == first["entry_hash"]
    assert first["entry_hash"] != second["entry_hash"]

    head = chain.head("tenant-a")
    assert head["head_id"] == second["id"]
    assert head["head_hash"] == second["entry_hash"]
    assert head["entries"] == 2


def test_head_of_unknown_tenant_is_genesis_without_row(store):
    head = AuditChain(store).head("tenant-empty")
    assert head == {"head_id": None, "head_hash": GENESIS_HASH, "entries": 0, "updated_at": None}


def test_verify_ok_and_reports_chain_start(store):
    chain = AuditChain(store)
    first = chain.append("tenant-a", "session.run", run_id="r1")
    second = chain.append("tenant-a", "session.run", run_id="r2")

    report = chain.verify("tenant-a")

    assert report["ok"] is True
    assert report["entries"] == 2
    assert report["broken_at"] is None and report["reason"] is None
    assert report["degraded"] is False
    assert report["head_hash"] == second["entry_hash"]
    assert report["chain_start_entry_id"] == first["id"]
    assert report["chain_start_at"] == first["ts"]
    assert report["uncovered_count"] == 0


def test_historical_rows_are_uncovered_and_never_backfilled(store, conn):
    # 0006 之前写入的行（普通 append，不走链）：迁移不回填，必须被统计为未覆盖。
    store.append("tenant-a", "query.completed", run_id="legacy-1")
    store.append("tenant-a", "query.completed", run_id="legacy-2")
    chain = AuditChain(store)
    first_chained = chain.append("tenant-a", "tool.call", resource="quote")

    report = chain.verify("tenant-a")

    assert report["ok"] is True
    assert report["entries"] == 1
    assert report["uncovered_count"] == 2
    assert report["chain_start_entry_id"] == first_chained["id"]
    legacy = conn.execute(
        "SELECT count(*) FROM audit_log WHERE entry_hash IS NULL AND prev_hash IS NULL"
    ).fetchone()[0]
    assert legacy == 2, "历史行必须保持 NULL（禁止伪造哈希）"


def test_verify_detects_tampered_detail(store, conn):
    chain = AuditChain(store)
    chain.append("tenant-a", "tool.call", resource="quote", detail={"ok": True})
    target = chain.append("tenant-a", "tool.call", resource="order", detail={"ok": True})

    _disable_append_only(conn)
    try:
        conn.execute(
            "UPDATE audit_log SET detail = %s::jsonb WHERE id = %s",
            ('{"ok": false}', target["id"]),
        )
    finally:
        _enable_append_only(conn)

    report = chain.verify("tenant-a")
    assert report["ok"] is False
    assert report["reason"] == "hash_mismatch"
    assert report["broken_at"] == target["id"]


def test_verify_detects_deleted_middle_row(store, conn):
    chain = AuditChain(store)
    chain.append("tenant-a", "tool.call", resource="a")
    middle = chain.append("tenant-a", "tool.call", resource="b")
    last = chain.append("tenant-a", "tool.call", resource="c")

    _disable_append_only(conn)
    try:
        conn.execute("DELETE FROM audit_log WHERE id = %s", (middle["id"],))
    finally:
        _enable_append_only(conn)

    report = chain.verify("tenant-a")
    assert report["ok"] is False
    assert report["reason"] == "prev_mismatch"
    assert report["broken_at"] == last["id"]


def test_verify_detects_truncated_tail(store, conn):
    chain = AuditChain(store)
    chain.append("tenant-a", "tool.call", resource="a")
    last = chain.append("tenant-a", "tool.call", resource="b")

    _disable_append_only(conn)
    try:
        conn.execute("DELETE FROM audit_log WHERE id = %s", (last["id"],))
    finally:
        _enable_append_only(conn)

    report = chain.verify("tenant-a")
    assert report["ok"] is False
    assert report["reason"] == "head_mismatch"
    assert report["broken_at"] is None


def test_append_only_trigger_blocks_app_side_mutation(store, conn):
    """应用侧没有改写通路：即便直接发 UPDATE，DB 也必须拒绝。"""
    created = AuditChain(store).append("tenant-a", "tool.call", resource="a")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE audit_log SET detail = '{}'::jsonb WHERE id = %s", (created["id"],))


# ---------------------------------------------------------------------------
# 隔离 / 并发
# ---------------------------------------------------------------------------
def test_chains_are_isolated_per_tenant(store):
    chain = AuditChain(store)
    chain.append("tenant-a", "tool.call", resource="a1")
    chain.append("tenant-b", "tool.call", resource="b1")
    chain.append("tenant-b", "tool.call", resource="b2")

    a = chain.verify("tenant-a")
    b = chain.verify("tenant-b")

    assert (a["ok"], a["entries"]) == (True, 1)
    assert (b["ok"], b["entries"]) == (True, 2)
    assert a["head_hash"] != b["head_hash"]
    assert chain.head("tenant-a")["entries"] == 1


def test_concurrent_appends_do_not_fork(store):
    chain = AuditChain(store)

    def _write(index: int) -> None:
        AuditChain(store).append("tenant-a", "tool.call", resource=f"tool-{index}", run_id=f"r-{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_write, range(8)))

    report = chain.verify("tenant-a")
    assert report["ok"] is True, report
    assert report["entries"] == 8
    assert chain.head("tenant-a")["entries"] == 8


# ---------------------------------------------------------------------------
# query.* 并入链（api._append -> AuditChain）
# ---------------------------------------------------------------------------
def test_query_audit_rows_enter_the_chain(store, monkeypatch):
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    chain = AuditChain(store)
    store.append("tenant-a", "query.completed", run_id="legacy")  # 迁移前的未入链行
    assert chain.verify("tenant-a")["uncovered_count"] == 1

    ok = audit_api._append(
        "tenant-a",
        "query.completed",
        user_id="user-1",
        resource="/v1/query",
        run_id="run-1",
        model="model-x",
        prompt_hash="p" * 32,
        detail={"tokens": {"total_tokens": 3}},
    )

    assert ok is True
    report = chain.verify("tenant-a")
    assert report["ok"] is True
    assert report["entries"] == 1
    assert report["uncovered_count"] == 1, "新事件不许再推高未覆盖数"


def test_consecutive_query_audits_link(store, conn, monkeypatch):
    monkeypatch.setattr(audit_api, "get_audit_store", lambda: store)
    for run in ("r1", "r2"):
        assert (
            audit_api._append(
                "tenant-a",
                "query.completed",
                user_id="user-1",
                resource="/v1/query",
                run_id=run,
                model=None,
                prompt_hash=None,
                detail={},
            )
            is True
        )

    rows = conn.execute("SELECT prev_hash, entry_hash FROM audit_log ORDER BY id ASC").fetchall()
    assert rows[0][0] == GENESIS_HASH
    assert rows[1][0] == rows[0][1], "第二条必须串在第一条之后"
    assert rows[1][1] != rows[0][1]
    assert AuditChain(store).verify("tenant-a")["ok"] is True


# ---------------------------------------------------------------------------
# 保留策略（只读）
# ---------------------------------------------------------------------------
def test_retention_plan_counts_old_rows_without_deleting(store, conn, monkeypatch):
    # retention_plan 默认拿进程级单例 store（连 .env 的库）：这里必须指向临时 schema。
    monkeypatch.setattr(retention_mod, "get_audit_store", lambda: store)
    chain = AuditChain(store)
    old_ts = datetime.now(timezone.utc) - timedelta(days=400)
    chain.append("tenant-a", "tool.call", resource="old", ts=old_ts)
    chain.append("tenant-a", "tool.call", resource="new")

    plan = retention_plan("tenant-a", older_than_days=365)

    assert plan["total_count"] == 2
    assert plan["archivable_count"] == 1
    assert plan["delete_performed"] is False
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 2, "规划不得删行"
    # 显式 ts（400 天前）也必须能通过校验：证明 timestamptz 往返不改变 hash。
    assert chain.verify("tenant-a")["ok"] is True
