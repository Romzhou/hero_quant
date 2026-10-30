"""W3 快照/PIT 离线契约：bars 规范化、内容哈希确定性、fail-closed 入参校验。

真 PG 的 snapshot/as-of 语义在 `tests/test_data_snapshot_pg.py`（PG 守卫）。
"""

from __future__ import annotations

import math

import pytest

from hero_quant.data.snapshot import (
    SnapshotStore,
    SnapshotUnavailable,
    SnapshotValidationError,
    capture_snapshot,
    normalize_bars,
    snapshot_digest,
)


def _bars() -> dict[str, list[dict[str, object]]]:
    return {
        "AAA": [
            {"date": "2024-01-02", "open": 10.0, "high": 10.8, "low": 9.8, "close": 10.5, "volume": 1000},
            {"date": "2024-01-01", "open": 9.5, "high": 10.1, "low": 9.4, "close": 10.0, "volume": 900},
        ],
        "BBB": [
            {"date": "2024-01-01", "open": 20.0, "high": 20.5, "low": 19.5, "close": 20.2, "volume": 500},
        ],
    }


def _d(value: str):
    from datetime import date

    return date.fromisoformat(value)


def test_normalize_bars_sorts_and_flattens():
    rows = normalize_bars(_bars(), "2024-01-01", "2024-01-05")
    assert [(row["symbol"], row["trade_date"].isoformat()) for row in rows] == [
        ("AAA", "2024-01-01"),
        ("AAA", "2024-01-02"),
        ("BBB", "2024-01-01"),
    ]
    assert rows[0]["close"] == 10.0


def test_normalize_bars_rejects_bad_inputs():
    cases = [
        ({}, "bars_empty"),
        ({"AAA": []}, "bars_empty:AAA"),
        ({"AAA": [{"date": "2024-01-01"}]}, "not_numeric"),
        ({"AAA": [{"date": "2024-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": -1}]}, "non_negative"),
        (
            {"AAA": [{"date": "2024-01-01", "open": 10, "high": 9, "low": 8, "close": 9, "volume": 1}]},
            "ohlc_inconsistent",
        ),
        (
            {
                "AAA": [
                    {"date": "2024-01-01", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
                    {"date": "2024-01-01", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
                ]
            },
            "duplicate_bar",
        ),
        (
            {"AAA": [{"date": "2023-12-31", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1}]},
            "out_of_window",
        ),
        (
            {"AAA": [{"date": "2024-01-01", "open": math.inf, "high": 10, "low": 10, "close": 10, "volume": 1}]},
            "not_finite",
        ),
    ]
    for bars, message in cases:
        with pytest.raises(SnapshotValidationError, match=message):
            normalize_bars(bars, "2024-01-01", "2024-01-05")  # type: ignore[arg-type]


def test_normalize_bars_rejects_non_mapping_and_bad_symbols():
    with pytest.raises(SnapshotValidationError, match="bars_empty"):
        normalize_bars("not-a-mapping", "2024-01-01", "2024-01-05")  # type: ignore[arg-type]
    with pytest.raises(SnapshotValidationError, match="symbol_must_be_str"):
        normalize_bars({1: _bars()["AAA"]}, "2024-01-01", "2024-01-05")  # type: ignore[dict-item]


def test_normalize_bars_accepts_dataframe_with_date_index():
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame(
        [{"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100}],
        index=pd.to_datetime(["2024-01-02"]),
    )
    frame.index.name = "date"
    rows = normalize_bars({"AAA": frame}, "2024-01-01", "2024-01-05")
    assert rows[0]["trade_date"].isoformat() == "2024-01-02"
    assert rows[0]["close"] == 10.2


def test_snapshot_digest_is_deterministic_and_content_addressed():
    rows = normalize_bars(_bars(), "2024-01-01", "2024-01-05")
    first_id, first_hash = snapshot_digest("acme", "fixture", "qfq", ["AAA", "BBB"], _d("2024-01-01"), _d("2024-01-05"), rows)
    second_id, second_hash = snapshot_digest("acme", "fixture", "qfq", ["BBB", "AAA"], _d("2024-01-01"), _d("2024-01-05"), rows)
    assert (first_id, first_hash) == (second_id, second_hash)
    assert first_id.startswith("snap_") and len(first_hash) == 64

    changed = [dict(row) for row in rows]
    changed[0]["close"] = changed[0]["close"] + 0.01
    third_id, third_hash = snapshot_digest("acme", "fixture", "qfq", ["AAA", "BBB"], _d("2024-01-01"), _d("2024-01-05"), changed)
    assert (third_id, third_hash) != (first_id, first_hash)

    other_tenant, _ = snapshot_digest("globex", "fixture", "qfq", ["AAA", "BBB"], _d("2024-01-01"), _d("2024-01-05"), rows)
    assert other_tenant != first_id


class _NeverPool:
    def connection(self):  # pragma: no cover - 非法入参不应触碰连接池
        raise AssertionError("pool must not be touched for invalid input")


def test_snapshot_store_requires_pool():
    with pytest.raises(ValueError, match="connection"):
        SnapshotStore(None)


def test_snapshot_store_from_empty_dsn_is_503():
    with pytest.raises(SnapshotUnavailable) as excinfo:
        SnapshotStore.from_dsn("")
    assert excinfo.value.status_code == 503


def test_record_snapshot_validates_before_io():
    store = SnapshotStore(_NeverPool())
    with pytest.raises(SnapshotValidationError, match="symbols_mismatch_bars"):
        store.record_snapshot("acme", "fixture", ["AAA"], "2024-01-01", "2024-01-05", _bars())
    with pytest.raises(SnapshotValidationError, match="symbols_empty_or_duplicate"):
        store.record_snapshot("acme", "fixture", "AAA", "2024-01-01", "2024-01-05", _bars())  # type: ignore[arg-type]
    with pytest.raises(SnapshotValidationError, match="knowable_at_before_trade_date"):
        store.record_snapshot(
            "acme", "fixture", ["AAA", "BBB"], "2024-01-01", "2024-01-05", _bars(), knowable_at="2023-12-31"
        )
    with pytest.raises(SnapshotValidationError, match="start_after_end"):
        store.record_snapshot("acme", "fixture", ["AAA", "BBB"], "2024-01-05", "2024-01-01", _bars())


class _Prov:
    source = "tencent"
    unit = "board_lots"
    adjust = "qfq"


class _Registry:
    def __init__(self, sources: dict[str, str] | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self.sources = sources or {}

    def get_bars(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        prov = _Prov()
        prov.source = self.sources.get(symbol, "tencent")
        return (
            [{"date": "2024-01-02", "open": 10, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100}],
            prov,
        )


class _StoreRecorder:
    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def record_snapshot(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"snapshot_id": "snap_recorded"}


def test_capture_snapshot_forwards_provenance():
    store = _StoreRecorder()
    registry = _Registry()
    result = capture_snapshot(store, "acme", registry, ["AAA"], "2024-01-01", "2024-01-05")
    assert result["snapshot_id"] == "snap_recorded"
    assert registry.calls == [("AAA", "2024-01-01", "2024-01-05")]
    args, kwargs = store.calls[0]
    assert args[0] == "acme"
    assert args[1] == "tencent"
    assert kwargs["adjust"] == "qfq"
    assert kwargs["meta"]["provenance"]["AAA"] == {
        "source": "tencent",
        "unit": "board_lots",
        "adjust": "qfq",
    }


def test_capture_snapshot_marks_mixed_sources():
    store = _StoreRecorder()
    registry = _Registry(sources={"AAA": "tencent", "BBB": "yahoo"})
    capture_snapshot(store, "acme", registry, ["AAA", "BBB"], "2024-01-01", "2024-01-05")
    _args, kwargs = store.calls[0]
    assert _args[1] == "mixed"
    assert kwargs["adjust"] == "qfq"

    with pytest.raises(SnapshotValidationError, match="symbols_empty_or_duplicate"):
        capture_snapshot(store, "acme", registry, ["AAA", "AAA"], "2024-01-01", "2024-01-05")
