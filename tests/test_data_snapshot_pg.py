"""W3 真 PG：快照幂等、内容寻址、as-of 只返回时点可见数据、跨租户不可见。

门控与隔离同 `tests/test_w4w8_pg.py`：PG 守卫不可用则整模块 skip；临时 schema
跑完 DROP CASCADE，不污染共享库。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
pytestmark = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "")

psycopg = pytest.importorskip("psycopg")

from hero_quant.data.snapshot import (  # noqa: E402
    SnapshotNotFound,
    SnapshotStore,
)
from hero_quant.db.runner import apply_migrations  # noqa: E402


def _conninfo() -> str:
    return pg_test_dsn("w3_snapshot_pg").replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture()
def schema():
    name = f"w3_{uuid.uuid4().hex[:10]}"
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


def _bars() -> dict[str, list[dict[str, object]]]:
    return {
        "AAA": [
            {"date": "2024-01-02", "open": 10.0, "high": 10.8, "low": 9.8, "close": 10.5, "volume": 1000},
            {"date": "2024-01-03", "open": 10.5, "high": 11.0, "low": 10.2, "close": 10.9, "volume": 1100},
            {"date": "2024-01-04", "open": 10.9, "high": 11.2, "low": 10.6, "close": 10.7, "volume": 900},
        ],
        "BBB": [
            {"date": "2024-01-02", "open": 20.0, "high": 20.5, "low": 19.5, "close": 20.2, "volume": 500},
            {"date": "2024-01-03", "open": 20.2, "high": 20.9, "low": 20.0, "close": 20.6, "volume": 600},
        ],
    }


def _record(store: SnapshotStore, tenant: str = "tenant-a", **kwargs) -> dict:
    params = {
        "tenant_id": tenant,
        "vendor": "fixture",
        "symbols": ["AAA", "BBB"],
        "start": "2024-01-01",
        "end": "2024-01-05",
        "bars_by_symbol": _bars(),
        "adjust": "qfq",
    }
    params.update(kwargs)
    return store.record_snapshot(**params)


def test_snapshot_is_content_addressed_and_idempotent(conn, schema):
    apply_migrations(conn)
    store = SnapshotStore(_Pool(schema))

    first = _record(store)
    assert first["created"] is True
    assert first["row_count"] == 5
    assert first["snapshot_id"].startswith("snap_")
    assert first["symbols"] == ["AAA", "BBB"]
    assert first["storage_uri"] == f"pg://pit_bars/{first['snapshot_id']}"

    again = _record(store)
    assert again["created"] is False
    assert again["snapshot_id"] == first["snapshot_id"]
    assert again["sha256"] == first["sha256"]

    changed_bars = _bars()
    changed_bars["AAA"][0]["close"] = 10.51
    revised = _record(store, bars_by_symbol=changed_bars)
    assert revised["created"] is True
    assert revised["snapshot_id"] != first["snapshot_id"]
    assert len(store.list_snapshots("tenant-a")) == 2


def test_as_of_only_returns_data_visible_at_that_time(conn, schema):
    apply_migrations(conn)
    store = SnapshotStore(_Pool(schema))
    snapshot = _record(store)

    assert [row["trade_date"] for row in store.as_of("tenant-a", snapshot["snapshot_id"], "AAA", "2024-01-03")] == [
        "2024-01-02",
        "2024-01-03",
    ]
    assert store.as_of("tenant-a", snapshot["snapshot_id"], "AAA", "2024-01-01") == []
    assert len(store.as_of("tenant-a", snapshot["snapshot_id"], "AAA", "2024-01-04")) == 3
    # 窗口下界过滤
    only_late = store.as_of(
        "tenant-a", snapshot["snapshot_id"], "AAA", "2024-01-04", start="2024-01-03"
    )
    assert [row["trade_date"] for row in only_late] == ["2024-01-03", "2024-01-04"]


def test_knowable_at_blocks_backfilled_data(conn, schema):
    apply_migrations(conn)
    store = SnapshotStore(_Pool(schema))
    snapshot = _record(store, knowable_at="2024-01-04")

    # 数据在 04 才可知：03 的 as-of 必须看不到任何 bar
    assert store.as_of("tenant-a", snapshot["snapshot_id"], "AAA", "2024-01-03") == []
    assert len(store.as_of("tenant-a", snapshot["snapshot_id"], "AAA", "2024-01-04")) == 3


def test_snapshot_cross_tenant_is_invisible(conn, schema):
    apply_migrations(conn)
    store = SnapshotStore(_Pool(schema))
    snapshot = _record(store, tenant="tenant-a")

    with pytest.raises(SnapshotNotFound):
        store.get_snapshot("tenant-b", snapshot["snapshot_id"])
    with pytest.raises(SnapshotNotFound):
        store.as_of("tenant-b", snapshot["snapshot_id"], "AAA", "2024-01-04")
    assert store.list_snapshots("tenant-b") == []
    assert store.list_snapshots("tenant-a", symbol="AAA")[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert store.list_snapshots("tenant-a", symbol="CCC") == []
