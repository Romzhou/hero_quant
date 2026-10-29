"""W4/W8 真 PG 集成：迁移 upgrade/幂等/旧库收编、tenant 隔离、幂等、审计只追加。

门控：`tests/conftest.py` 的 PG 守卫不可用则整模块 skip（本机无 DSN 时不红）。
隔离：每个用例创建独立临时 schema（search_path），结束 DROP SCHEMA CASCADE，
不污染共享库；RLS 断言在 superuser（BYPASSRLS）下自动 skip。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
pytestmark = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "")

psycopg = pytest.importorskip("psycopg")

from hero_quant.audit.store import AuditStore  # noqa: E402
from hero_quant.db.loader import load_migrations  # noqa: E402
from hero_quant.db.runner import (  # noqa: E402
    apply_migrations,
    current_version,
    drift_report,
    status,
)
from hero_quant.session.store import (  # noqa: E402
    RunNotFound,
    SessionNotFound,
    SessionStore,
)


def _conninfo() -> str:
    return pg_test_dsn("w4w8_pg").replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture()
def schema():
    name = f"w4w8_{uuid.uuid4().hex[:10]}"
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
    """每次取连接都落到测试 schema（SET 在 autocommit 下会话级生效）。"""

    def __init__(self, schema: str):
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


def _is_superuser(conn) -> bool:
    row = conn.execute("SELECT current_setting('is_superuser')").fetchone()
    return bool(row and row[0] == "on")


def test_upgrade_empty_database_and_idempotent(conn):
    migrations = load_migrations()
    applied = apply_migrations(conn)
    assert applied == [m.version for m in migrations]
    assert current_version(conn) == migrations[-1].version
    assert apply_migrations(conn) == []
    assert drift_report(conn) == []

    snapshot = status(conn)
    assert snapshot["db_version"] == migrations[-1].version
    assert snapshot["pending"] == []
    assert snapshot["drift"] == []


def test_old_database_is_adopted(conn):
    # 旧库形态：checkpoints 由内联 DDL 建好，缺 run_text/version；无 schema_version。
    conn.execute(
        """
        CREATE TABLE checkpoints (
          tenant text NOT NULL,
          thread text NOT NULL,
          seq int NOT NULL,
          checkpoint jsonb NOT NULL,
          expires_at timestamptz,
          PRIMARY KEY (tenant, thread, seq)
        )
        """
    )
    assert apply_migrations(conn) == [m.version for m in load_migrations()]
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'checkpoints'"
        ).fetchall()
    }
    assert {"run_text", "version"} <= columns


def test_session_tenant_isolation(conn, schema):
    apply_migrations(conn)
    store = SessionStore(_Pool(schema))

    session = store.create_session("tenant-a", "user-1", "调研")
    assert store.get_session("tenant-a", session["id"])["title"] == "调研"

    with pytest.raises(SessionNotFound):
        store.get_session("tenant-b", session["id"])
    with pytest.raises(SessionNotFound):
        store.append_message("tenant-b", session["id"], "user", "越权")
    assert store.list_sessions("tenant-b") == []
    assert store.list_messages("tenant-a", session["id"]) == []


def test_rls_blocks_cross_tenant_reads(conn, schema):
    apply_migrations(conn)
    if _is_superuser(conn):
        pytest.skip("RLS 断言需要非 superuser 连接（BYPASSRLS 会绕过策略）")

    SessionStore(_Pool(schema)).create_session("tenant-a", "user-1")
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant', %s, true)", ("tenant-b",))
        count = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    assert count == 0, "RLS 必须在 tenant-b 上下文隐藏 tenant-a 的会话"


def test_tool_call_idempotency_and_run_resume(conn, schema):
    apply_migrations(conn)
    store = SessionStore(_Pool(schema))
    session = store.create_session("tenant-a", "user-1")
    run = store.start_run("tenant-a", session["id"])

    first, created_first = store.record_tool_call(
        "tenant-a", run["id"], "get_bars", "hash1", "run:step1", result_ref="s3://x"
    )
    assert created_first is True
    done = store.complete_tool_call("tenant-a", "run:step1", "SUCCESS", result_ref="s3://x")
    assert done["status"] == "SUCCESS"

    second, created_second = store.record_tool_call(
        "tenant-a", run["id"], "get_bars", "hash1", "run:step1"
    )
    assert created_second is False
    assert second["id"] == first["id"]
    assert second["status"] == "SUCCESS", "重放不得覆盖首次结果"

    # 消息 + 重启（新 store / 新连接）后续跑
    store.append_message("tenant-a", session["id"], "user", "第一问", trace_id="t1")
    store.append_message("tenant-a", session["id"], "assistant", "第一答", trace_id="t1")
    restarted = SessionStore(_Pool(schema))
    resumed = restarted.resume_session("tenant-a", session["id"], after_id=1)
    assert [m["content"] for m in resumed["messages"]] == ["第一答"]
    assert resumed["last_event_id"] == 2
    assert resumed["degraded"] is False

    with pytest.raises(RunNotFound):
        restarted.get_run("tenant-b", run["id"])


def test_audit_is_append_only_and_queryable_by_run(conn, schema):
    apply_migrations(conn)
    audit = AuditStore(_Pool(schema))
    audit.append("tenant-a", "query.start", user_id="u1", run_id="run-1", model="gpt-4o")
    audit.append("tenant-a", "tool.call", user_id="u1", run_id="run-1", detail={"tool": "get_bars"})
    audit.append("tenant-a", "query.end", user_id="u2", run_id="run-2", result_hash="abc123")

    chain = audit.trace("tenant-a", "run-1")
    assert [event["action"] for event in chain] == ["query.start", "tool.call"]
    assert audit.query("tenant-a", user_id="u2")[0]["action"] == "query.end"
    assert audit.query("tenant-b") == []

    for sql in ("UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)

    rows = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    assert rows == 3
