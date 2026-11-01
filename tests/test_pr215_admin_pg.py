"""PR-215 真 PG 验收：管理后台聚合查询 / 跨租户权限判定 / 租户内 RLS。

跑法（唯一入口，避免手拼 DSN）：

    python scripts/run_pg_tests.py tests/test_pr215_admin_pg.py

隔离：每个模块一个临时 schema（apply_migrations 建表）+ 用完 DROP CASCADE。
RLS 用例需要非 superuser 角色：优先挑一个 NOSUPERUSER 登录角色并 SET ROLE；
没有则 skip 并给原因（不静默绿灯）。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
pytestmark = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "")

psycopg = pytest.importorskip("psycopg")

from hero_quant.admin.store import AdminScopeUnavailable, AdminStore  # noqa: E402
from hero_quant.db.runner import apply_migrations  # noqa: E402

_TENANT_A = "tenant-w7-a"
_TENANT_B = "tenant-w7-b"


def _conninfo() -> str:
    return pg_test_dsn("w7_admin_pg").replace("postgresql+psycopg://", "postgresql://", 1)


class _Pool:
    """每次 connection() 新建连接（线程安全）；可选 SET ROLE 到非 superuser 角色。"""

    def __init__(self, schema: str, *, role: str | None = None) -> None:
        self._dsn = _conninfo()
        self._schema = schema
        self._role = role

    @contextmanager
    def connection(self):
        conn = psycopg.connect(self._dsn, autocommit=True)
        try:
            conn.execute(f'SET search_path TO "{self._schema}"')
            if self._role:
                conn.execute(f'SET ROLE "{self._role}"')
            yield conn
        finally:
            conn.close()


def _find_rls_role(admin) -> str | None:
    row = admin.execute(
        "SELECT rolname FROM pg_roles"
        " WHERE NOT rolsuper AND NOT rolbypassrls AND rolcanlogin"
        " AND rolname <> current_user ORDER BY rolname LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


@pytest.fixture(scope="module")
def schema():
    name = "w7_admin_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(_conninfo(), autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{name}"')
    except psycopg.Error as exc:  # pragma: no cover - 权限不足时给明确 skip
        admin.close()
        pytest.skip(f"cannot create temp schema for isolation: {type(exc).__name__}")
    role = None
    try:
        admin.execute(f'SET search_path TO "{name}"')
        apply_migrations(admin)
        role = _find_rls_role(admin)
        if role is not None:
            admin.execute(f'GRANT USAGE ON SCHEMA "{name}" TO "{role}"')
            admin.execute(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{name}" TO "{role}"'
            )
        yield name, role
    finally:
        admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        admin.close()


@pytest.fixture(scope="module")
def seeded(schema):
    name, _role = schema
    conn = psycopg.connect(_conninfo(), autocommit=True)
    conn.execute(f'SET search_path TO "{name}"')
    try:
        conn.execute(
            "INSERT INTO runs (id, tenant_id, status, kind, created_at) VALUES"
            " ('run-a-old', %s, 'queued', 'agent', now() - interval '90 seconds'),"
            " ('run-a-new', %s, 'queued', 'agent', now() - interval '10 seconds'),"
            " ('run-a-done', %s, 'succeeded', 'agent', now()),"
            " ('run-b-done', %s, 'failed', 'backtest', now())",
            (_TENANT_A, _TENANT_A, _TENANT_A, _TENANT_B),
        )
        conn.execute(
            "INSERT INTO audit_log (tenant_id, action, ts) VALUES"
            " (%s, 'query.completed', now()),"
            " (%s, 'tool.call', now()),"
            " (%s, 'admin.toggle', now())",
            (_TENANT_A, _TENANT_A, _TENANT_B),
        )
        yield name
    finally:
        conn.close()


def test_platform_overview_counts_all_tenants(seeded):
    store = AdminStore(_Pool(seeded))
    data = store.overview(None)
    assert data["runs"]["total"] == 4
    assert data["runs"]["by_status"] == {"queued": 2, "succeeded": 1, "failed": 1}
    assert data["audit"] == {"entries": 3}
    assert data["queue"]["oldest_queued_run_id"] == "run-a-old"
    assert data["queue"]["oldest_queued_wait_seconds"] >= 80


def test_tenant_overview_is_scoped(seeded):
    store = AdminStore(_Pool(seeded))
    data = store.overview(_TENANT_A)
    assert data["runs"]["total"] == 3
    assert data["runs"]["by_status"] == {"queued": 2, "succeeded": 1}
    assert data["audit"] == {"entries": 2}
    assert data["queue"]["oldest_queued_run_id"] == "run-a-old"


def test_tenants_union_runs_and_audit(seeded):
    rows = AdminStore(_Pool(seeded)).tenants(limit=50)
    by_tenant = {row["tenant_id"]: row for row in rows}
    assert set(by_tenant) == {_TENANT_A, _TENANT_B}
    assert by_tenant[_TENANT_A]["runs"] == 3
    assert by_tenant[_TENANT_B]["runs"] == 1
    assert by_tenant[_TENANT_A]["last_activity_at"] is not None
    assert by_tenant[_TENANT_B]["last_activity_at"] is not None


def test_audit_total_is_tenant_scoped(seeded):
    store = AdminStore(_Pool(seeded))
    assert store.audit_total(_TENANT_A) == 2
    assert store.audit_total(_TENANT_B) == 1


def test_no_queued_runs_returns_none_not_zero(seeded):
    store = AdminStore(_Pool(seeded))
    assert store.overview(_TENANT_B)["queue"] is None


def test_cross_tenant_denied_for_unprivileged_role(seeded, schema):
    name, role = schema
    if role is None:
        pytest.skip("no non-superuser login role available for RLS probe")
    store = AdminStore(_Pool(name, role=role))
    with pytest.raises(AdminScopeUnavailable):
        store.overview(None)


def test_tenant_view_still_works_under_rls(seeded, schema):
    name, role = schema
    if role is None:
        pytest.skip("no non-superuser login role available for RLS probe")
    store = AdminStore(_Pool(name, role=role))
    data = store.overview(_TENANT_A)
    assert data["runs"]["total"] == 3, "SET LOCAL 后应只看得到本租户"
