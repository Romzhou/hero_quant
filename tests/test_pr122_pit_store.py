"""PR-122-a：PIT Parquet 存储（Parquet 分区 + DuckDB as-of）离线契约。

所有用例一律使用 `tmp_path`：不读 HERO_PIT_ROOT、不碰 /root/hero-data/pit，
也不因真实目录不可用而 skip（读不到真实目录不是跳过测试的理由）。

未来函数反证的关键场景：同一 `(tenant, symbol, trade_date)` 分区会被「原始快照」和
「修订快照」同时命中，parquet 行内 snapshot_id 保证 as_of 不会串快照、也不会用修订值
覆盖原始值——这是 PG 版唯一约束 (snapshot_id, symbol, trade_date) 的等价物。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from hero_quant.data import pit_store
from hero_quant.data.pit_store import PitParquetStore, PitStoreUnavailable
from hero_quant.data.snapshot import SnapshotNotFound, SnapshotValidationError

_TENANT = "tenant-a"
_VENDOR = "vendor-x"
_ADJUST = "qfq"


def _bar(day: str, close: float) -> dict[str, object]:
    """测试构造值（非真实行情）：open=close，high/low 包住实体，volume 固定。"""
    price = float(close)
    return {
        "date": day,
        "open": price,
        "high": price + 0.5,
        "low": price - 0.5,
        "close": price,
        "volume": 1000,
    }


def _bars() -> dict[str, list[dict[str, object]]]:
    return {
        "AAA": [_bar("2024-01-02", 10.5), _bar("2024-01-03", 10.7)],
        "BBB": [_bar("2024-01-02", 20.5)],
    }


def test_write_daily_round_trip(tmp_path):
    store = PitParquetStore(tmp_path)
    manifest = store.write_daily(_TENANT, _VENDOR, _ADJUST, _bars())

    assert manifest["created"] is True
    assert manifest["row_count"] == 3
    assert manifest["rows"] == 3
    assert manifest["partitions"] == 3
    assert manifest["bytes"] > 0
    assert manifest["snapshot_id"].startswith("snap_")
    assert len(manifest["sha256"]) == 64
    assert manifest["storage_uri"].startswith("file://")
    assert manifest["symbols"] == ["AAA", "BBB"]
    assert manifest["knowable_at"] is None

    part = tmp_path / "tenant=tenant-a" / "symbol=AAA" / "trade_date=2024-01-02" / "part-000.parquet"
    assert part.is_file()
    # 清单原子写：不得残留临时文件。
    assert [p.name for p in (tmp_path / "_index").iterdir() if p.name.startswith(".")] == []

    snapshot = store.get_snapshot(_TENANT, manifest["snapshot_id"])
    assert snapshot["created"] is False
    assert snapshot["sha256"] == manifest["sha256"]
    assert snapshot["vendor"] == _VENDOR
    assert snapshot["adjust"] == _ADJUST

    rows = store.as_of(_TENANT, manifest["snapshot_id"], "AAA", "2024-01-03")
    assert [row["trade_date"].isoformat() for row in rows] == ["2024-01-02", "2024-01-03"]
    assert [row["close"] for row in rows] == [10.5, 10.7]
    assert rows[0]["knowable_at"].isoformat() == "2024-01-02"
    assert set(rows[0]) == {"symbol", "trade_date", "knowable_at", "open", "high", "low", "close", "volume"}
    assert rows[0]["symbol"] == "AAA"

    # as_of 早于最新 bar 只返回当时可见的根。
    assert len(store.as_of(_TENANT, manifest["snapshot_id"], "AAA", "2024-01-02")) == 1
    # start/end/limit 与 PG 版同口径。
    assert len(store.as_of(_TENANT, manifest["snapshot_id"], "AAA", "2024-01-03", start="2024-01-03")) == 1
    assert store.as_of(_TENANT, manifest["snapshot_id"], "AAA", "2024-01-03", limit=1)[0]["trade_date"].isoformat() == "2024-01-02"
    # 快照里没有的标的返回空，不报错（与 PG 版一致）。
    assert store.as_of(_TENANT, manifest["snapshot_id"], "CCC", "2024-01-03") == []


def test_as_of_excludes_future_revision(tmp_path):
    store = PitParquetStore(tmp_path)
    original = store.write_daily(
        _TENANT,
        _VENDOR,
        _ADJUST,
        {"AAA": [_bar("2024-01-02", 10.0), _bar("2024-01-03", 10.2)]},
    )
    # 修订快照：01-02 的收盘被改成 10.6，但 2024-01-10 才可知。
    revised = store.write_daily(
        _TENANT,
        _VENDOR,
        _ADJUST,
        {"AAA": [_bar("2024-01-02", 10.6), _bar("2024-01-03", 10.2)]},
        knowable_at="2024-01-10",
    )
    assert revised["snapshot_id"] != original["snapshot_id"]

    # 未来函数反证：修订行在 2024-01-05 不可见（整份快照当时都还不可知）。
    assert store.as_of(_TENANT, revised["snapshot_id"], "AAA", "2024-01-05") == []
    # 到 knowable_at 之后才可见，且看到的是修订值。
    visible = store.as_of(_TENANT, revised["snapshot_id"], "AAA", "2024-01-10")
    assert [row["close"] for row in visible] == [10.6, 10.2]
    # 修订不得覆盖原始快照：同一分区里两份快照各自自洽。
    vintage = store.as_of(_TENANT, original["snapshot_id"], "AAA", "2024-01-05")
    assert [row["close"] for row in vintage] == [10.0, 10.2]
    assert [row["knowable_at"].isoformat() for row in vintage] == ["2024-01-02", "2024-01-03"]

    # 同一快照的 trade_date 过滤仍先于 knowable_at：窗口内不可知即空。
    assert store.as_of(_TENANT, revised["snapshot_id"], "AAA", "2024-01-10", start="2024-01-01", end="2024-01-02") != []
    assert store.as_of(_TENANT, revised["snapshot_id"], "AAA", "2024-01-02", start="2024-01-01", end="2024-01-02") == []


def test_same_content_idempotent_and_one_bar_change_rotates_snapshot_id(tmp_path):
    store = PitParquetStore(tmp_path)
    first = store.write_daily(_TENANT, _VENDOR, _ADJUST, _bars())
    second = store.write_daily(_TENANT, _VENDOR, _ADJUST, _bars())

    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["sha256"] == first["sha256"]
    assert second["created"] is False
    assert store.dataset_stats(_TENANT)["snapshots"] == 1
    assert store.dataset_stats(_TENANT)["rows"] == 3

    changed = _bars()
    changed["AAA"][1] = _bar("2024-01-03", 10.9)  # 只改一根 bar
    third = store.write_daily(_TENANT, _VENDOR, _ADJUST, changed)

    assert third["snapshot_id"] != first["snapshot_id"]
    assert third["sha256"] != first["sha256"]
    assert third["created"] is True
    assert store.dataset_stats(_TENANT)["snapshots"] == 2
    assert len(store.list_snapshots(_TENANT)) == 2
    # 旧快照读到的仍是旧值（内容不变式）。
    old = store.as_of(_TENANT, first["snapshot_id"], "AAA", "2024-01-03")
    assert [row["close"] for row in old] == [10.5, 10.7]
    new = store.as_of(_TENANT, third["snapshot_id"], "AAA", "2024-01-03")
    assert [row["close"] for row in new] == [10.5, 10.9]


def test_list_snapshots_and_dataset_stats_filters(tmp_path):
    store = PitParquetStore(tmp_path)
    first = store.write_daily(_TENANT, "vendor-x", _ADJUST, _bars())
    # 第二份只含 AAA（BBB 缺席），用来验证 symbol 过滤确实按快照清单生效。
    changed = {"AAA": [_bar("2024-01-02", 10.5), _bar("2024-01-03", 10.9)]}
    second = store.write_daily(_TENANT, "vendor-y", _ADJUST, changed)
    other = store.write_daily("tenant-b", "vendor-x", _ADJUST, {"CCC": [_bar("2024-01-02", 30.0)]})

    assert {item["snapshot_id"] for item in store.list_snapshots(_TENANT)} == {
        first["snapshot_id"],
        second["snapshot_id"],
    }
    assert [item["snapshot_id"] for item in store.list_snapshots(_TENANT, vendor="vendor-y")] == [second["snapshot_id"]]
    assert [item["snapshot_id"] for item in store.list_snapshots(_TENANT, symbol="BBB")] == [first["snapshot_id"]]
    assert {item["snapshot_id"] for item in store.list_snapshots(_TENANT, symbol="AAA")} == {
        first["snapshot_id"],
        second["snapshot_id"],
    }
    assert len(store.list_snapshots(_TENANT, limit=1)) == 1
    assert store.list_snapshots("tenant-c") == []

    scoped = store.dataset_stats(_TENANT)
    assert scoped["snapshots"] == 2
    assert scoped["symbols"] == ["AAA", "BBB"]
    everything = store.dataset_stats()
    assert everything["snapshots"] == 3
    assert everything["tenants"] == ["tenant-a", "tenant-b"]
    assert everything["files"] == everything["partitions"] > 0
    assert everything["bytes"] > 0
    assert other["snapshot_id"] in {item["snapshot_id"] for item in store.list_snapshots("tenant-b")}


def test_missing_root_fails_fast_without_mkdir(tmp_path):
    missing = tmp_path / "not-created"
    with pytest.raises(PitStoreUnavailable) as excinfo:
        PitParquetStore(missing)
    assert excinfo.value.reason == "pit_root_missing"
    assert excinfo.value.status_code == 503
    assert not missing.exists()  # 禁止 mkdir 兜底


def test_not_writable_root_fails_fast(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    # 容器以 root 运行，chmod 0555 对 os.access(W_OK) 不生效；直接打桩可写性判定。
    monkeypatch.setattr(pit_store, "_is_writable", lambda path: False)
    with pytest.raises(PitStoreUnavailable) as excinfo:
        PitParquetStore(root)
    assert excinfo.value.reason == "pit_root_not_writable"


def test_root_that_is_a_file_fails_fast(tmp_path):
    path = tmp_path / "root-is-file"
    path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(PitStoreUnavailable) as excinfo:
        PitParquetStore(path)
    assert excinfo.value.reason == "pit_root_not_writable"


@pytest.mark.parametrize("tenant", ["", "   ", "../evil", "a/b", "a b", ".", "..", "-leading-dash"])
def test_illegal_tenant_rejected(tmp_path, tenant):
    store = PitParquetStore(tmp_path)
    with pytest.raises(SnapshotValidationError, match="tenant_id"):
        store.write_daily(tenant, _VENDOR, _ADJUST, {"AAA": [_bar("2024-01-02", 10.5)]})


def test_empty_bars_rejected(tmp_path):
    store = PitParquetStore(tmp_path)
    with pytest.raises(SnapshotValidationError, match="bars_empty"):
        store.write_daily(_TENANT, _VENDOR, _ADJUST, {})
    with pytest.raises(SnapshotValidationError, match="bars_empty:AAA"):
        store.write_daily(_TENANT, _VENDOR, _ADJUST, {"AAA": []})


def test_cross_tenant_and_traversal_snapshot_ids_do_not_leak(tmp_path):
    store = PitParquetStore(tmp_path)
    manifest = store.write_daily(_TENANT, _VENDOR, _ADJUST, _bars())

    with pytest.raises(SnapshotNotFound):
        store.get_snapshot("tenant-b", manifest["snapshot_id"])
    with pytest.raises(SnapshotNotFound):
        store.as_of("tenant-b", manifest["snapshot_id"], "AAA", "2024-01-03")
    with pytest.raises(SnapshotValidationError, match="snapshot_id_invalid"):
        store.get_snapshot(_TENANT, "../../etc/passwd")
    with pytest.raises(SnapshotNotFound):
        store.get_snapshot(_TENANT, "snap_" + "0" * 20)


def test_symbol_with_path_separator_is_percent_encoded(tmp_path):
    store = PitParquetStore(tmp_path)
    manifest = store.write_daily(_TENANT, _VENDOR, _ADJUST, {"BTC/USDT": [_bar("2024-01-02", 100.0)]})

    assert (tmp_path / "tenant=tenant-a" / "symbol=BTC%2FUSDT" / "trade_date=2024-01-02" / "part-000.parquet").is_file()
    rows = store.as_of(_TENANT, manifest["snapshot_id"], "BTC/USDT", "2024-01-02")
    assert [row["symbol"] for row in rows] == ["BTC/USDT"]


def test_sample_row_count_and_disk_size_within_budget(tmp_path):
    """样本体积门：约 3 年真实样本规模（750 根日线），打印实测并锁 100M 上限。"""
    store = PitParquetStore(tmp_path)
    bars: list[dict[str, object]] = []
    day = 0
    while len(bars) < 750:
        current = date(2023, 1, 2) + timedelta(days=day)
        day += 1
        if current.weekday() >= 5:  # 周末 bar 会被 data.quality 判为 weekend_bar(block)
            continue
        # 收盘在 10.00~10.19 之间缓慢变化：避免 flat_close_run 与 return_outlier。
        bars.append(_bar(current.isoformat(), 10.0 + (len(bars) % 20) * 0.01))
    manifest = store.write_daily(_TENANT, _VENDOR, _ADJUST, {"AAA": bars})
    stats = store.dataset_stats(_TENANT)

    print(
        f"pit-sample rows={stats['rows']} files={stats['files']} "
        f"bytes={stats['bytes']} manifest_bytes={manifest['bytes']}"
    )
    assert stats["rows"] == 750
    assert stats["files"] == 750
    assert math.isfinite(float(stats["bytes"]))
    assert stats["bytes"] < 100 * 1024 * 1024

    rows = store.as_of(_TENANT, manifest["snapshot_id"], "AAA", "2026-12-31", limit=5000)
    assert len(rows) == 750
    assert rows[-1]["trade_date"].isoformat() == bars[-1]["date"]
