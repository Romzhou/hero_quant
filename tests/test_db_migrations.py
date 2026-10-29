"""PR-133 迁移框架离线用例：脚本切分 / 注册表校验 / 状态机 / drift 阻断。

不依赖 PostgreSQL：执行器通过可注入的连接替身验证 SQL 与事务时序；
真实 DDL/RLS 由 `tests/test_w4w8_pg.py` 在 PG 守卫下跑。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from hero_quant.db.loader import Migration, MigrationLoadError, load_migrations
from hero_quant.db.runner import (
    MigrationDriftError,
    MigrationError,
    apply_migrations,
    applied_versions,
    current_version,
    drift_report,
    pending_migrations,
    status,
)
from hero_quant.db.sql import split_sql_statements


# --------------------------------------------------------------------------- 替身


class _Cursor:
    def __init__(self, rows=None, rowcount: int = 0):
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self.description = None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """最小 PG 替身：只理解 schema_version / to_regclass 两类查询。"""

    def __init__(self, applied=None, tables=None):
        self.applied = list(applied or [])
        self.tables = set(tables or ())
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    @contextmanager
    def transaction(self):
        yield

    def commit(self):
        self.commits += 1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        norm = " ".join(sql.split()).lower()

        if norm.startswith("select to_regclass"):
            name = str(params[0]).split(".")[-1] if params else ""
            return _Cursor([(name if name in self.tables else None,)])

        if "create table if not exists schema_version" in norm:
            self.tables.add("schema_version")
            return _Cursor()

        if norm.startswith("select version, name, checksum") and "from schema_version" in norm:
            return _Cursor([(v, n, c) for v, n, c in self.applied])

        if norm.startswith("insert into schema_version"):
            version, name, checksum = params[0], params[1], params[2]
            self.applied.append((version, name, checksum))
            return _Cursor(rowcount=1)

        return _Cursor()


def _all_tables() -> set[str]:
    return {
        "schema_version",
        "checkpoints",
        "checkpoints_legacy",
        "factors",
        "purchases",
        "dedup",
        "tool_call_dedup",
        "sessions",
        "messages",
        "runs",
        "tool_calls",
        "audit_log",
    }


# --------------------------------------------------------------------------- SQL 切分


def test_split_sql_handles_quotes_comments_and_dollar_blocks():
    sql = """
    -- leading comment; with semicolon
    CREATE TABLE t (a text DEFAULT 'x;y');
    INSERT INTO t VALUES ('it''s; ok');
    DO $$ BEGIN PERFORM 1; END $$;
    CREATE OR REPLACE FUNCTION f() RETURNS text AS $body$
    BEGIN
      RETURN 'a;b';
    END;
    $body$ LANGUAGE plpgsql;
    """
    statements = split_sql_statements(sql)
    assert len(statements) == 4
    assert statements[0].startswith("-- leading comment") or statements[0].startswith("CREATE TABLE")
    assert "it''s; ok" in statements[1]
    assert statements[2].startswith("DO $$")
    assert "$body$" in statements[3]


def test_split_sql_returns_empty_for_comment_only():
    assert split_sql_statements("-- just a comment;\n-- another;") == []


def test_split_sql_rejects_non_str():
    with pytest.raises(TypeError):
        split_sql_statements(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 注册表


def test_shipped_migrations_are_contiguous_and_cover_all_tables():
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    joined = "\n".join(stmt for m in migrations for stmt in m.statements)
    for table in _all_tables() - {"schema_version"}:
        assert f"TABLE IF NOT EXISTS {table}" in joined, f"baseline missing {table}"
    # 工具幂等与审计只追加是 PR-132/171 的硬约束
    assert "UNIQUE (tenant_id, idempotency_key)" in joined
    assert "audit_log_append_only" in joined
    assert all(m.checksum() and len(m.checksum()) == 64 for m in migrations)


def test_load_migrations_rejects_non_contiguous(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationLoadError, match="contiguous"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_bad_filename(tmp_path):
    (tmp_path / "1_a.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationLoadError, match="invalid migration filename"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_empty_dir(tmp_path):
    with pytest.raises(MigrationLoadError, match="no migration files"):
        load_migrations(tmp_path)


# --------------------------------------------------------------------------- 执行器


def _migration(version: int, checksum_suffix: str = "") -> Migration:
    return Migration(
        version=version,
        name=f"m{version}",
        statements=(f"SELECT {version}{checksum_suffix};",),
        source=f"{version:04d}_m{version}.sql",
    )


def test_apply_from_empty_database_applies_all():
    conn = _FakeConn()
    applied = apply_migrations(conn)
    assert applied == [m.version for m in load_migrations()]
    assert current_version(conn) == len(applied)
    assert pending_migrations(conn) == []


def test_apply_is_idempotent():
    conn = _FakeConn()
    apply_migrations(conn)
    first_count = len(conn.executed)
    assert apply_migrations(conn) == []
    # 第二次只做 schema_version 探测/读取 + advisory lock，不应再执行迁移 DDL
    assert len(conn.executed) > first_count
    assert not any("CREATE TABLE IF NOT EXISTS sessions" in sql for sql, _ in conn.executed[first_count:])


def test_apply_rejects_checksum_drift():
    baseline = load_migrations()[0]
    conn = _FakeConn(applied=[(1, baseline.name, "0" * 64)], tables={"schema_version"})
    with pytest.raises(MigrationDriftError, match="checksum mismatch"):
        apply_migrations(conn)


def test_apply_rejects_database_ahead_of_code():
    conn = _FakeConn(applied=[(99, "future", "f" * 64)], tables={"schema_version"})
    with pytest.raises(MigrationDriftError, match="database ahead of code"):
        apply_migrations(conn)


def test_apply_rolls_back_failed_migration():
    conn = _FakeConn()
    real_execute = conn.execute

    def flaky(sql, params=None):
        if sql.strip().startswith("BROKEN"):
            raise RuntimeError("syntax error")
        return real_execute(sql, params)

    conn.execute = flaky  # type: ignore[method-assign]
    broken = Migration(version=1, name="broken", statements=("BROKEN;",), source="0001_broken.sql")
    with pytest.raises(RuntimeError, match="syntax error"):
        apply_migrations(conn, [broken], use_advisory_lock=False)
    assert applied_versions(conn) == {}


def test_pending_reports_only_unapplied():
    migrations = [_migration(1), _migration(2), _migration(3)]
    conn = _FakeConn(applied=[(1, "m1", migrations[0].checksum())], tables={"schema_version", "sessions"})
    pending = pending_migrations(conn, migrations)
    assert [m.version for m in pending] == [2, 3]


def test_status_exposes_versions_and_drift():
    migrations = [_migration(1)]
    conn = _FakeConn(tables=_all_tables())
    snapshot = status(conn, migrations)
    assert snapshot["code_version"] == 1
    assert snapshot["db_version"] == 0
    assert snapshot["pending"] == [{"version": 1, "name": "m1"}]
    assert snapshot["drift"] == []


def test_drift_report_flags_missing_tables():
    conn = _FakeConn(applied=[(1, "m1", _migration(1).checksum())], tables={"schema_version"})
    problems = drift_report(conn, [_migration(1)])
    assert any("missing table: sessions" in item for item in problems)


def test_empty_registry_is_rejected():
    with pytest.raises(MigrationError, match="empty"):
        apply_migrations(_FakeConn(), [], use_advisory_lock=False)
