"""Task6 TDD: bench 合成守卫与 disclosure 去重 + validation 排序校验。

覆盖：
- bench.run_batch allow_synthetic=False 必须抛错（fail-closed，不被强制覆写）
- synthetic_fallback 标记 provenance=synthetic_fallback
- disclosure 仅保留 get_disclosure/_build_pit_disclosure，其余 5 别名 DeprecationWarning
- validation DatetimeIndex 去重/排序校验
"""
import tempfile
import warnings

import pandas as pd
import pytest


def test_bench_allow_synthetic_false_blocks():
    """allow_synthetic=False 必须抛错，不应被强制覆写为 True。"""
    from hero_quant.backtest.bench import run_batch

    # 显式 False 应抛 ValueError/PITViolation（fail-closed）
    with pytest.raises((ValueError, Exception)):
        run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=False)
    # 默认 False（不传参）亦应抛错
    with pytest.raises((ValueError, Exception)):
        run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"])


def test_bench_allow_synthetic_true_passes():
    """allow_synthetic=True 允许合成路径并产出结果。"""
    from hero_quant.backtest.bench import run_batch

    with tempfile.TemporaryDirectory() as tmp:
        res = run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], output_dir=tmp, allow_synthetic=True)
        assert "AAPL" in res
        assert "alpha" in res["AAPL"]
        assert "benchmark" in res["AAPL"]


def test_bench_synthetic_fallback_marks_provenance():
    """engine 失败兜底分支必须标记 provenance=synthetic_fallback。"""
    from unittest.mock import patch

    from hero_quant.backtest import bench as bench_mod
    from hero_quant.backtest.bench import run_batch

    # 仅策略引擎失败，基准正常 — 验证 fallback 标记
    with patch.object(bench_mod.BacktestEngine, "run", side_effect=RuntimeError("engine boom")):
        with tempfile.TemporaryDirectory() as tmp:
            res = run_batch(["AAPL"], dates=["2024-01-01"], output_dir=tmp, allow_synthetic=True)
            assert res["AAPL"]["provenance"] == "synthetic_fallback"
            # 兜底指标应为零化，且仍含 disclosure 等诚实字段
            assert res["AAPL"]["sharpe"] == 0.0
            assert "disclosure" in res["AAPL"]
            assert "non-PIT" in res["AAPL"]["disclosure"] or "non_pit" in res["AAPL"]


def test_disclosure_canonical_no_warning():
    """get_disclosure/_build_pit_disclosure 为正典，不应告警。"""
    from hero_quant.backtest.bench import _build_pit_disclosure, get_disclosure

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        get_disclosure([{"pit": True}])
        assert len(w) == 0, f"get_disclosure should not warn, got {w}"
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _build_pit_disclosure([{"pit": True}])
        assert len(w) == 0


def test_disclosure_deprecated_aliases_warn():
    """5 个重复 disclosure 别名必须以 DeprecationWarning 包装。"""
    from hero_quant.backtest.bench import (
        build_disclosure,
        build_pit_disclosure,
        get_bench_disclosure,
        get_pit_disclosure,
        news_disclosure,
    )

    aliases = [
        ("build_disclosure", build_disclosure),
        ("get_pit_disclosure", get_pit_disclosure),
        ("build_pit_disclosure", build_pit_disclosure),
        ("get_bench_disclosure", get_bench_disclosure),
        ("news_disclosure", news_disclosure),
    ]
    for name, fn in aliases:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            out = fn([{"pit": True}])
            assert len(w) == 1, f"{name} should emit exactly one warning"
            assert issubclass(w[0].category, DeprecationWarning), f"{name} warning category mismatch"
            assert "deprecated" in str(w[0].message).lower() and "get_disclosure" in str(w[0].message)
            assert isinstance(out, str) and len(out) > 0


def test_validation_duplicate_datetimeindex_raises():
    """DatetimeIndex 去重校验：重复时间戳必须抛 ValidationError。"""
    from hero_quant.backtest.validation import ValidationError, validate

    idx = pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-02"])
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]}, index=idx)
    with pytest.raises(ValidationError, match="duplicated"):
        validate(df)


def test_validation_unsorted_datetimeindex_raises():
    """DatetimeIndex 排序校验：未按时间递增必须抛 ValidationError（fail-closed）。"""
    from hero_quant.backtest.validation import ValidationError, validate

    idx = pd.to_datetime(["2024-01-03", "2024-01-01", "2024-01-02"])
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]}, index=idx)
    with pytest.raises(ValidationError, match="sorted|monotonic|not sorted"):
        validate(df)


def test_validation_sorted_datetimeindex_passes():
    """已排序的 DatetimeIndex 应通过校验。"""
    from hero_quant.backtest.validation import validate

    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]}, index=idx)
    # 不应抛错
    assert validate(df) is None


def test_validation_non_datetimeindex_no_sort_raise():
    """非 DatetimeIndex（如 RangeIndex）不应触发排序校验。"""
    from hero_quant.backtest.validation import validate

    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
    # RangeIndex 时不校验排序，仅校验空/价格
    assert validate(df) is None
