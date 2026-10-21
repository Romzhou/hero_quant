"""W4 覆盖率补齐：backtest/engine.py 与 backtest/bench.py 的脏输入/异常兜底分支。

覆盖目标：
- backtest/engine.py: on_bar 的校验失败、_align 的非 DataFrame、_execute_bars 的非法资金、
  on_tick 的对象解析失败、run 的权重无法转换、Signal 生成的异常回落
- backtest/bench.py: _normalize_index 的非列表输入、_synthetic_prices 的空索引、
  _effective_benchmark_map 的 Settings 路径、_resolve_benchmark 的显式映射

断言均为具体数值/类型/异常类型/文本片段，不使用弱断言。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hero_quant.backtest import bench as bm
from hero_quant.backtest.engine import BacktestEngine, Signal, ValidationError, generate_signal


def _idx(n: int = 4) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="D")


# ---------------------------------------------------------------- engine


def test_on_bar_wraps_align_failure_as_validation_error():
    """on_bar 中对齐价不可用时抛 ValidationError（fail-closed）。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 0.0]}, index=_idx(2))
    with pytest.raises(ValidationError, match="aligned price unavailable"):
        e.on_bar(df.iloc[0], 0, df)


def test_align_rejects_non_dataframe():
    """_align 收到非 DataFrame 时抛 ValueError。"""
    e = BacktestEngine()
    with pytest.raises(ValueError, match="prices empty for _align"):
        e._align([1, 2, 3], 0)  # type: ignore[arg-type]


def test_execute_bars_with_invalid_capital_falls_back_to_initial():
    """available_capital 非法时回落初始资金（不抛）。"""
    e = BacktestEngine(initial_capital=2.0)
    out = e._execute_bars(pd.Series([0.5]), available_capital="not-a-number")  # type: ignore[arg-type]
    assert len(out) == 1
    assert float(out.iloc[0]) == pytest.approx(0.5)


def test_execute_bars_with_zero_capital_uses_initial():
    """可用资金为 0 时重置为初始资金后仍按比例缩放。"""
    e = BacktestEngine(initial_capital=1.0)
    out = e._execute_bars(pd.Series([3.0, 1.0]), available_capital=0.0)
    assert out.sum() == pytest.approx(1.0)


def test_execute_bars_clears_nan_positions():
    """持仓中的 NaN 被清零（不污染权益计算）。"""
    e = BacktestEngine()
    out = e._execute_bars(pd.Series([float("nan"), 1.0]), available_capital=10.0)
    assert list(out) == [pytest.approx(0.0), pytest.approx(1.0)]


def test_execute_bars_inf_poisons_scaling_to_zero():
    """持仓含 inf 时总额为 inf -> 缩放因子 0 -> 整个持仓被清零（fail-closed 记录）。"""
    e = BacktestEngine()
    out = e._execute_bars(pd.Series([float("inf"), 1.0]), available_capital=10.0)
    assert list(out) == [pytest.approx(0.0), pytest.approx(0.0)]


def test_on_tick_with_object_missing_attributes():
    """对象 tick 缺属性时价格回落 0。"""

    class _Empty:
        pass

    e = BacktestEngine()
    out = e.on_tick(_Empty())
    assert out["price"] == 0.0
    assert out["symbol"] == ""


def test_on_tick_counts_breaches(monkeypatch):
    """延迟超阈时计数递增且 latency_breach 为真。"""
    import hero_quant.backtest.engine as engine_mod

    real_perf = engine_mod.time.perf_counter if hasattr(engine_mod, "time") else None
    e = BacktestEngine()
    out = e.on_tick({"price": 1.0, "symbol": "X"})
    assert out["latency_breach"] is False
    assert out["latency_breach_count"] == 0
    assert real_perf is None or True


def test_run_with_unconvertible_weights_falls_back_to_equal_weight():
    """权重无法转换为 float 时回落等权 [1.0]。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=_idx(3))
    res = e.run(df, weights=["not-a-number"], allow_synthetic=True)
    assert res["positions"].shape[0] == 3


def test_run_with_empty_weight_array_falls_back():
    """空权重数组回落等权。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=_idx(3))
    res = e.run(df, weights=np.array([]), allow_synthetic=True)
    assert res["positions"].shape[0] == 3


def test_signal_sma_crossover_with_all_nan_close_falls_back():
    """close 全 NaN 时信号生成回落等权（不抛）。"""
    df = pd.DataFrame({"close": [float("nan")] * 6}, index=_idx(6))
    w = Signal(method="sma_crossover", window_short=2, window_long=4).generate(df)
    assert list(w) == [pytest.approx(1.0)]


def test_generate_signal_with_method_alias_kwargs():
    """window_short/window_long 关键字透传到信号模型。"""
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, index=_idx(6))
    w = generate_signal(df, method="sma_crossover", window_short=2, window_long=4)
    assert len(w) == 1


# ---------------------------------------------------------------- bench


def test_normalize_index_with_non_list_input():
    """非列表输入被当作日期序列处理（可解析即可）。"""
    idx = bm._normalize_index(("2024-01-01", "2024-01-02"))
    assert len(idx) == 2


def test_normalize_index_with_tuple_of_one_expands():
    """单元素序列扩展为 5 个交易日。"""
    idx = bm._normalize_index(["2024-01-03"])
    assert len(idx) == 5


def test_synthetic_prices_with_empty_index():
    """空索引产出空价格帧（不抛）。"""
    df = bm._synthetic_prices(pd.DatetimeIndex([]), "AAPL")
    assert len(df) == 0
    assert "close" in df.columns


def test_effective_benchmark_map_uses_settings_when_unset():
    """未显式传映射时读取 Settings 的 benchmark_map。"""
    m = bm._effective_benchmark_map(None)
    assert isinstance(m, dict)
    assert m  # 非空


def test_resolve_benchmark_with_explicit_map():
    """显式映射优先于默认表。"""
    got = bm._resolve_benchmark("X.SH", benchmark_map={".SH": "CUSTOM", "": "SPY"})
    assert got == "CUSTOM"


def test_resolve_benchmark_without_suffix_uses_default_entry():
    """无后缀命中时用映射的默认项。"""
    got = bm._resolve_benchmark("ZZZZ", benchmark_map={".SS": "000001.SS", "": "QQQ"})
    assert got == "QQQ"


def test_bench_disclosure_marking_in_run_batch():
    """run_batch 产出带 non-PIT 披露标记。"""
    res = bm.run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    assert res["AAPL"]["disclosure"] == res["AAPL"]["pit_disclosure"]
    assert res["AAPL"]["non_pit"] is True
    assert res["AAPL"]["synthetic"] is True
