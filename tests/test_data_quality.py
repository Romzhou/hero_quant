"""PR-123 数据质量门：四类检查的命中/不命中、reason 稳定性、坏数据零写入。

纯函数部分直接打 `data.quality`；接线部分用假 pool（不连 PG）断言阻断发生在
取连接/INSERT 之前，好数据仍照常落库。
"""

from __future__ import annotations

import math
from contextlib import contextmanager, nullcontext
from datetime import date
from typing import Any

import pytest

from hero_quant.data.quality import (
    SEVERITY_BLOCK,
    SEVERITY_WARN,
    DataQualityError,
    QualityThresholds,
    assert_quality,
    check_bars,
)
from hero_quant.data.snapshot import (
    SnapshotQualityError,
    SnapshotStore,
    SnapshotValidationError,
    normalize_bars,
)

# 2024-01-01 是周一；01-06/01-07 是周六/周日。
_WEEK = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
# 6 个连续「非周末」交易日：默认 max_flat_run=5，用来验证连平命中。
_SIX_WEEKDAYS = [*_WEEK, "2024-01-08"]
# 10 个「非周末」交易日：横截面覆盖率检查的 union 需 >= min_union_days(5)。
_TEN_DAYS = [*_WEEK, "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12"]


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _row(symbol: str, day: str, close: float, volume: float = 1000) -> dict[str, Any]:
    return {"symbol": symbol, "trade_date": _d(day), "close": close, "volume": volume}


def _codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


# --------------------------------------------------------------------------- 日历


def test_weekend_bar_blocks_and_weekday_passes():
    weekend = check_bars([_row("AAA", "2024-01-05", 10.0), _row("AAA", "2024-01-06", 10.1)])
    assert weekend.ok is False
    assert weekend.reason == "weekend_bar"
    assert weekend.blocks[0].trade_date == _d("2024-01-06")
    assert weekend.blocks[0].count == 1

    weekday = check_bars([_row("AAA", "2024-01-04", 10.0), _row("AAA", "2024-01-05", 10.1)])
    assert weekday.ok is True
    assert "weekend_bar" not in _codes(weekday)


def test_calendar_gap_blocks_and_full_coverage_passes():
    calendar = [_d(day) for day in _WEEK]
    full = [_row("AAA", day, 10.0 + index / 10) for index, day in enumerate(_WEEK)]
    assert check_bars(full, calendar=calendar).ok is True

    gapped = [row for row in full if row["trade_date"] != _d("2024-01-03")]
    report = check_bars(gapped, calendar=calendar)
    assert report.ok is False
    assert "coverage_gap" in _codes(report)
    gap = next(finding for finding in report.findings if finding.code == "coverage_gap")
    assert gap.count == 1
    assert gap.detail.startswith("count=1")
    assert "20% > 5%" in gap.detail


def test_missing_head_and_tail_are_reported():
    calendar = [_d(day) for day in _WEEK]
    rows = [_row("AAA", day, 10.0) for day in _WEEK[1:-1]]
    # 首尾缺失必须有显式窗口：不给 start/end 时窗口取 bar 自身首尾，「首尾」无定义。
    report = check_bars(rows, start="2024-01-01", end="2024-01-05", calendar=calendar)
    assert {"missing_head", "missing_tail"} <= _codes(report)
    # 同日按 code 升序：coverage_gap < missing_head，故 reason 取 coverage_gap。
    assert report.reason == "coverage_gap"


def test_calendar_violation_blocks_dates_outside_calendar():
    # 2024-01-03 视为休市日：不在 calendar 内，bar 却落在该日。
    rows = [_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 10.1)]
    report = check_bars(rows, start="2024-01-02", end="2024-01-04", calendar=[_d("2024-01-02"), _d("2024-01-04")])
    assert "calendar_violation" in _codes(report)
    assert report.reason == "calendar_violation"
    assert report.blocks[0].trade_date == _d("2024-01-03")


# ------------------------------------------------------------------------- 异常值


def test_return_outlier_blocks_and_small_moves_pass():
    spike = [_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 16.0)]
    report = check_bars(spike)
    assert report.reason == "return_outlier"
    assert report.blocks[0].trade_date == _d("2024-01-03")
    assert report.blocks[0].count == 1

    calm = [_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 10.4)]
    assert check_bars(calm).ok is True


def test_flat_close_run_blocks_and_moving_series_passes():
    flat = [_row("AAA", day, 10.0) for day in _SIX_WEEKDAYS]
    report = check_bars(flat)
    assert report.reason == "flat_close_run"
    assert report.blocks[0].count == 6
    assert report.blocks[0].severity == SEVERITY_BLOCK

    moving = [_row("AAA", day, price) for day, price in zip(_SIX_WEEKDAYS, (10.0, 10.2, 10.1, 9.9, 10.3, 10.0))]
    assert check_bars(moving).ok is True


def test_flat_close_run_with_zero_volume_only_warns():
    suspended = [_row("AAA", day, 10.0, volume=0) for day in _SIX_WEEKDAYS]
    report = check_bars(suspended)
    assert report.ok is True
    severities = {finding.code: finding.severity for finding in report.findings}
    assert severities["flat_close_run"] == SEVERITY_WARN


def test_negative_volume_blocks():
    rows = [_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 10.1, volume=-5)]
    report = check_bars(rows)
    assert report.reason == "negative_volume"
    assert report.blocks[0].trade_date == _d("2024-01-03")


def test_all_zero_volume_warns_without_blocking():
    rows = [_row("AAA", "2024-01-02", 10.0, volume=0), _row("AAA", "2024-01-03", 10.1, volume=0)]
    report = check_bars(rows)
    assert report.ok is True
    assert [finding.code for finding in report.warnings] == ["volume_all_zero"]
    assert report.warnings[0].count == 2


def test_non_finite_or_non_positive_price_blocks():
    rows = [
        _row("AAA", "2024-01-02", 10.0),
        {"symbol": "AAA", "trade_date": _d("2024-01-03"), "close": math.inf, "volume": 100},
        {"symbol": "BBB", "trade_date": _d("2024-01-02"), "close": 0.0, "volume": 100},
    ]
    report = check_bars(rows)
    assert {"price_not_finite", "price_not_positive"} <= _codes(report)


def test_malformed_rows_are_reported_not_dropped():
    rows = [{"symbol": "AAA", "trade_date": "not-a-date", "close": 1.0, "volume": 1}, "oops"]
    report = check_bars(rows)
    assert "bar_malformed" in _codes(report)
    assert {finding.symbol for finding in report.findings} == {None, "AAA"}


def test_symbol_without_any_bar_blocks():
    report = check_bars([_row("AAA", "2024-01-02", 10.0)], symbols=["AAA", "ZZZ"])
    assert report.reason == "symbol_missing"
    assert report.blocks[0].symbol == "ZZZ"


# ------------------------------------------------------------------------- 跨源


def test_cross_source_divergence_blocks_and_tolerance_is_a_parameter():
    rows = [_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 10.1)]
    reference = {"AAA": [_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 10.5)]}
    report = check_bars(rows, reference=reference)
    assert report.reason == "cross_source_divergence"
    assert report.blocks[0].trade_date == _d("2024-01-03")

    loose = check_bars(rows, reference=reference, thresholds=QualityThresholds(cross_source_tolerance=0.1))
    assert "cross_source_divergence" not in _codes(loose)


def test_cross_source_without_overlap_warns():
    report = check_bars([_row("AAA", "2024-01-02", 10.0)], reference={"AAA": [_row("AAA", "2024-02-01", 10.0)]})
    assert report.ok is True
    assert [finding.code for finding in report.warnings] == ["cross_source_no_overlap"]


# --------------------------------------------------------------- 横截面覆盖率


def _series(symbol: str, days: list[str], start_close: float = 10.0) -> list[dict[str, Any]]:
    return [_row(symbol, day, start_close + index * 0.1) for index, day in enumerate(days)]


def test_cross_coverage_gap_vs_peers_blocks():
    rows = _series("AAA", _TEN_DAYS) + _series("BBB", _TEN_DAYS[:2])
    report = check_bars(rows)
    assert report.ok is False
    assert report.reason == "coverage_gap_vs_peers"
    finding = report.blocks[0]
    assert finding.symbol == "BBB"
    assert finding.count == 8  # 10 天并集里缺 8 天
    assert "20%" in finding.detail and "union (10 days)" in finding.detail
    # reason 稳定：重复跑同一输入结果一致。
    assert check_bars(rows).reason == report.reason


def test_cross_coverage_close_passes():
    rows = _series("AAA", _TEN_DAYS) + _series("BBB", _TEN_DAYS[:9])
    report = check_bars(rows)
    assert report.ok is True
    assert "coverage_gap_vs_peers" not in _codes(report)
    assert "coverage_partial_vs_peers" not in _codes(report)  # 9/10 = 90%，边界上不报


def test_cross_coverage_partial_only_warns():
    rows = _series("AAA", _TEN_DAYS) + _series("BBB", _TEN_DAYS[:8])
    report = check_bars(rows)
    assert report.ok is True
    assert [finding.code for finding in report.warnings] == ["coverage_partial_vs_peers"]
    assert report.warnings[0].symbol == "BBB"


def test_cross_coverage_at_threshold_only_warns():
    """边界是严格小于：coverage == min_cross_coverage(50%) 只 warn，不 block。"""
    rows = _series("AAA", _TEN_DAYS) + _series("BBB", _TEN_DAYS[:5])
    report = check_bars(rows)
    assert report.ok is True
    assert [finding.code for finding in report.warnings] == ["coverage_partial_vs_peers"]


def test_cross_coverage_skipped_for_single_symbol():
    report = check_bars(_series("AAA", _TEN_DAYS))
    assert report.findings == ()


def test_cross_coverage_skipped_below_min_union_days():
    rows = _series("AAA", _TEN_DAYS[:4]) + _series("BBB", _TEN_DAYS[:1])
    assert check_bars(rows).findings == ()  # union 只有 4 天 < min_union_days(5)


def test_cross_coverage_thresholds_are_parameters():
    rows = _series("AAA", _TEN_DAYS) + _series("BBB", _TEN_DAYS[:8])
    strict = check_bars(rows, thresholds=QualityThresholds(min_cross_coverage=0.85))
    assert strict.reason == "coverage_gap_vs_peers"


def test_quality_thresholds_reject_bad_cross_coverage_values():
    with pytest.raises(ValueError, match="min_cross_coverage_out_of_range"):
        QualityThresholds(min_cross_coverage=1.5)
    with pytest.raises(ValueError, match="partial_coverage_below_min_cross_coverage"):
        QualityThresholds(min_cross_coverage=0.9, partial_coverage=0.5)
    with pytest.raises(ValueError, match="min_union_days_must_be_at_least_1"):
        QualityThresholds(min_union_days=0)


# -------------------------------------------------------------- reason 与 detail


def test_reason_is_the_first_block_in_symbol_date_code_order():
    rows = [
        _row("BBB", "2024-01-03", 10.0),
        _row("BBB", "2024-01-04", 18.0),  # return_outlier
        _row("AAA", "2024-01-05", 10.0),
        _row("AAA", "2024-01-06", 10.1),  # weekend_bar（周六）
    ]
    report = check_bars(rows)
    # symbol 优先于日期：AAA 的周六 bar 排在 BBB 的暴涨之前。
    assert report.reason == "weekend_bar"
    assert report.codes == ("return_outlier", "weekend_bar")

    with pytest.raises(DataQualityError) as excinfo:
        assert_quality(report)
    assert excinfo.value.reason == report.reason
    assert excinfo.value.codes == report.codes
    assert "data_quality_error: weekend_bar" in str(excinfo.value)


def test_assert_quality_returns_clean_report():
    report = check_bars([_row("AAA", "2024-01-02", 10.0), _row("AAA", "2024-01-03", 10.2)])
    assert report.ok is True
    assert assert_quality(report) is report


def test_finding_detail_never_echoes_whole_table():
    rows: list[dict[str, Any]] = []
    price = 10.0
    for index in range(20):
        price = price * 2 if index % 2 else price * 0.4
        rows.append(_row("AAA", f"2024-03-{index + 1:02d}", price))
    report = check_bars(rows)
    outliers = [finding for finding in report.findings if finding.code == "return_outlier"]
    assert len(outliers) == 1  # 同 (code, symbol) 聚合，条数与表长无关
    assert outliers[0].count >= 10
    assert outliers[0].detail.split("samples=")[1].count(",") == report.thresholds.max_samples - 1


# ------------------------------------------------------------- record_snapshot 接线


_SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "tenant_id",
    "vendor",
    "symbols",
    "start_date",
    "end_date",
    "adjust",
    "fetched_at",
    "row_count",
    "sha256",
    "storage_uri",
    "meta",
)


class _Cursor:
    def __init__(self, columns: tuple[str, ...] = (), row: Any = None) -> None:
        self.description = [(name,) for name in columns]
        self._row = row

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return []


class _Conn:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    def transaction(self):
        return nullcontext()

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        if sql.startswith("INSERT INTO data_snapshots"):
            self._pool.inserts.append(params)
            # RETURNING 的列序与 INSERT 参数序一致，直接当结果行喂回去。
            return _Cursor(_SNAPSHOT_COLUMNS, params)
        self._pool.statements.append(sql)
        return _Cursor()

    def executemany(self, sql: str, seq: Any) -> _Cursor:
        self._pool.statements.append(sql)
        self._pool.bar_rows.append(len(list(seq)))
        return _Cursor()


class _FakePool:
    """假 pool：只记录 SQL，不连 PG；坏数据用例断言 connections == 0。"""

    def __init__(self) -> None:
        self.connections = 0
        self.statements: list[str] = []
        self.inserts: list[Any] = []
        self.bar_rows: list[int] = []

    @contextmanager
    def connection(self):
        self.connections += 1
        yield _Conn(self)


def _good_bars() -> dict[str, list[dict[str, Any]]]:
    return {
        "AAA": [
            {"date": "2024-01-02", "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.2, "volume": 1000},
            {"date": "2024-01-03", "open": 10.2, "high": 10.6, "low": 10.1, "close": 10.5, "volume": 1100},
        ],
        "BBB": [
            {"date": "2024-01-02", "open": 20.0, "high": 20.3, "low": 19.8, "close": 20.1, "volume": 500},
        ],
    }


def _weekend_bars() -> dict[str, list[dict[str, Any]]]:
    return {
        "AAA": [
            {"date": "2024-01-05", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 1000},
            {"date": "2024-01-06", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 900},
        ]
    }


def test_record_snapshot_blocks_bad_data_before_any_write():
    pool = _FakePool()
    store = SnapshotStore(pool)
    with pytest.raises(SnapshotQualityError) as excinfo:
        # 窗口必须包含周六 01-06：窗口外的 bar 由 normalize_bars 先拦（不是质量门的活）。
        store.record_snapshot("acme", "fixture", ["AAA"], "2024-01-01", "2024-01-08", _weekend_bars())

    error = excinfo.value
    assert isinstance(error, SnapshotValidationError)  # 既有捕获路径不变
    assert error.reason == "weekend_bar"
    assert error.codes == ("weekend_bar",)
    assert error.status_code == 400
    assert "rows=" in error.detail
    # 坏数据不得产生 snapshot_id：连连接池都没碰过，更不会有 INSERT。
    assert pool.connections == 0
    assert pool.inserts == []


def _cross_gap_bars() -> dict[str, list[dict[str, Any]]]:
    def bar(day: str, close: float) -> dict[str, Any]:
        return {"date": day, "open": close, "high": close * 1.02, "low": close * 0.98, "close": close, "volume": 1000}

    return {
        "AAA": [bar(day, 10.0 + index * 0.1) for index, day in enumerate(_TEN_DAYS)],
        "BBB": [bar(day, 20.0 + index * 0.1) for index, day in enumerate(_TEN_DAYS[:2])],
    }


def test_record_snapshot_blocks_cross_coverage_gap_without_write():
    """record_snapshot 路径没有 calendar，完整性只能靠横截面覆盖率——必须在写入前拦住。"""
    pool = _FakePool()
    store = SnapshotStore(pool)
    with pytest.raises(SnapshotQualityError) as excinfo:
        store.record_snapshot("acme", "fixture", ["AAA", "BBB"], "2024-01-01", "2024-01-12", _cross_gap_bars())

    error = excinfo.value
    assert isinstance(error, SnapshotValidationError)
    assert error.reason == "coverage_gap_vs_peers"
    assert error.codes == ("coverage_gap_vs_peers",)
    assert "BBB" in error.detail
    assert pool.connections == 0
    assert pool.inserts == []


def test_record_snapshot_still_writes_clean_data():
    pool = _FakePool()
    store = SnapshotStore(pool)
    result = store.record_snapshot("acme", "fixture", ["AAA", "BBB"], "2024-01-01", "2024-01-05", _good_bars())

    assert result["created"] is True
    assert result["row_count"] == 3
    assert len(pool.inserts) == 1
    assert pool.bar_rows == [3]


def test_existing_snapshot_fixture_passes_default_gate():
    """tests/test_data_snapshot.py 的样例：01-01 周一 / 01-02 周二，标的根数不齐。"""
    bars = {
        "AAA": [
            {"date": "2024-01-02", "open": 10.0, "high": 10.8, "low": 9.8, "close": 10.5, "volume": 1000},
            {"date": "2024-01-01", "open": 9.5, "high": 10.1, "low": 9.4, "close": 10.0, "volume": 900},
        ],
        "BBB": [
            {"date": "2024-01-01", "open": 20.0, "high": 20.5, "low": 19.5, "close": 20.2, "volume": 500},
        ],
    }
    rows = normalize_bars(bars, "2024-01-01", "2024-01-05")
    report = check_bars(rows, symbols=["AAA", "BBB"], start="2024-01-01", end="2024-01-05")
    # 默认阈值下不误伤：缺口检查需显式 calendar，短窗口 + 覆盖不齐不算坏数据。
    assert report.ok is True
    assert report.findings == ()
