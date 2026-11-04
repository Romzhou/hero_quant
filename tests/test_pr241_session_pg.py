"""PR-241（批次16）会话级事件入审计链 + 「历史行」不被误判为篡改。

跑法（唯一入口）：
    python scripts/run_pg_tests.py tests/test_pr241_session_pg.py
或本地全量：
    scripts/test_local.sh tests/test_pr241_session_pg.py
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
pytestmark = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "")

psycopg = pytest.importorskip("psycopg")

from hero_quant.audit import producers as producers_mod  # noqa: E402
from hero_quant.audit.chain import AuditChain  # noqa: E402
from hero_quant.audit.producers import session_run_hook  # noqa: E402
from hero_quant.audit.store import AuditStore  # noqa: E402
from hero_quant.db.runner import apply_migrations  # noqa: E402
from hero_quant.session.store import SessionStore  # noqa: E402

_TENANT = "tenant-session-a"


def _conninfo() -> str:
    return pg_test_dsn("w2_session_audit_pg").replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture()
def schema():
    name = f"w2s_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(_conninfo(), autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(f"cannot create temp schema: {type(exc).__name__}")
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
        apply_migrations(connection)
        yield connection
    finally:
        connection.close()


class _Pool:
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


def test_session_lifecycle_enters_chain_and_verifies(schema, conn, monkeypatch):
    pool = _Pool(schema)
    audit_store = AuditStore(pool)
    # 生产里 producers 用进程级 store；测试把它指到临时 schema，避免写进共享库
    monkeypatch.setattr(producers_mod, "get_audit_store", lambda: audit_store)
    chain = AuditChain(audit_store)
    store = SessionStore(pool, audit_hook=session_run_hook())

    session = store.create_session(_TENANT, "user-1", title="t")
    run = store.start_run(_TENANT, session["id"])
    store.finish_run(_TENANT, run["id"], "succeeded")

    report = chain.verify(_TENANT)
    assert report["ok"] is True, report
    assert report["entries"] == 3
    assert report["uncovered_count"] == 0
    assert report["chain_start_entry_id"] is not None

    with pool.connection() as c:
        rows = c.execute(
            "SELECT action, entry_hash, detail FROM audit_log ORDER BY id ASC"
        ).fetchall()
    assert [r[0] for r in rows] == ["session.run", "session.run", "session.run"]
    assert all(r[1] for r in rows)
    phases = [r[2].get("phase") for r in rows]
    assert phases == ["created", "started", "finished"]
    assert rows[2][2].get("status") == "succeeded"


def test_legacy_rows_without_hash_are_not_reported_as_tampering(schema, conn, monkeypatch):
    """历史行（entry_hash IS NULL）只计入 uncovered_count，不判成 broken。"""
    pool = _Pool(schema)
    audit_store = AuditStore(pool)
    monkeypatch.setattr(producers_mod, "get_audit_store", lambda: audit_store)
    chain = AuditChain(audit_store)
    # 模拟迁移前的历史行：直接 INSERT，哈希列留空
    conn.execute(
        "INSERT INTO audit_log (tenant_id, action, detail) VALUES (%s, 'legacy.query', '{}'::jsonb)",
        (_TENANT,),
    )
    legacy_id = conn.execute("SELECT id FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]

    store = SessionStore(pool, audit_hook=session_run_hook())
    session = store.create_session(_TENANT, "user-1")

    report = chain.verify(_TENANT)
    assert report["ok"] is True, report
    assert report["broken_at"] is None
    assert report["uncovered_count"] == 1
    assert report["chain_start_entry_id"] > legacy_id
    assert report["entries"] == 1  # 只统计入链的行


def test_hook_failure_does_not_break_session_write(schema, conn):
    def _boom(_event):
        raise RuntimeError("audit down")

    store = SessionStore(_Pool(schema), audit_hook=_boom)
    session = store.create_session(_TENANT, "user-1")
    assert session["id"]

    with _Pool(schema).connection() as c:
        count = c.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    assert count == 0  # 审计失败就什么都没写，不产生半条
