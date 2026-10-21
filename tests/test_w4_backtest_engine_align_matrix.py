"""W4 覆盖率补齐：backtest/engine.py 的 _align / _price_matrix / 指标边界分支。

覆盖目标（src/hero_quant/backtest/engine.py）：
- _align：负索引归零、末 Bar 回落、多资产分支、open 优先于 close、无 close/open 的宽表回落、
  非法价格抛错
- _price_matrix：无候选列时的 close 兜底 / 整体数值化兜底 / 非正价 / 全 NaN 抛 DataFeedError
- run()：无 close 列但有资产列、close 全 NaN、非法 costs 字符串
- _compute_turnover_rate：首日换手补齐与空仓场景
- _drawdown_episodes_html：非 Timestamp 索引
- Signal.generate：sma_crossover 在列缺失时的异常回落

断言均为具体数值/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hero_quant.backtest.engine import (
    BacktestEngine,
    DataFeedError,
    Signal,
    generate_signal,
)


def _idx(n: int = 5) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="D")


# ---------------------------------------------------------------- _align


def test_align_negative_index_is_clamped_to_zero():
    """负索引归零（边界保护，不越界）。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=_idx(3))
    out = e._align(df, -1)
    assert float(out.iloc[0]) == pytest.approx(11.0)


def test_align_uses_next_day_open_when_present():
    """有 open 列时用次日 open 作为可执行价。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0], "open": [9.5, 10.5, 11.5]}, index=_idx(3))
    out = e._align(df, 0)
    assert float(out.iloc[0]) == pytest.approx(10.5)


def test_align_uses_next_day_close_without_open():
    """无 open 列时用次日 close。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=_idx(3))
    out = e._align(df, 0)
    assert float(out.iloc[0]) == pytest.approx(11.0)


def test_align_last_bar_falls_back_to_own_close():
    """末 Bar 无次日，回落至当 Bar 的 close。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=_idx(3))
    out = e._align(df, 2)
    assert float(out.iloc[0]) == pytest.approx(12.0)


def test_align_multi_asset_returns_all_columns():
    """多资产时返回每资产一条对齐价。"""
    e = BacktestEngine()
    df = pd.DataFrame({"A": [10.0, 11.0], "B": [20.0, 21.0]}, index=_idx(2))
    out = e._align(df, 0)
    assert set(out.index) == {"A", "B"}
    assert float(out["A"]) == pytest.approx(11.0)
    assert float(out["B"]) == pytest.approx(21.0)


def test_align_rejects_non_positive_price():
    """非正可执行价抛错（fail-closed，不注入 0 价）。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 0.0]}, index=_idx(2))
    with pytest.raises(ValueError, match="non-positive"):
        e._align(df, 0)


def test_align_wide_table_without_close_uses_first_numeric():
    """无 close/open 的宽表回落为首个有效数值。"""
    e = BacktestEngine()
    df = pd.DataFrame({"px1": [10.0, 11.0], "px2": [20.0, 21.0]}, index=_idx(2))
    out = e._align(df, 0)
    assert float(out.iloc[0]) == pytest.approx(11.0)


def test_align_empty_prices_raises():
    """空价格表在 _align 直接抛错。"""
    e = BacktestEngine()
    with pytest.raises(ValueError, match="prices empty for _align"):
        e._align(pd.DataFrame(), 0)


# ---------------------------------------------------------------- _price_matrix


def test_price_matrix_single_close_column():
    """单 close 列识别为单资产路径。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    mat, is_multi = e._price_matrix(df)
    assert is_multi is False
    assert mat.shape == (2, 1)


def test_price_matrix_multi_asset_detected():
    """多列价格识别为多资产路径。"""
    e = BacktestEngine()
    df = pd.DataFrame({"A": [10.0, 11.0], "B": [20.0, 21.0]}, index=_idx(2))
    mat, is_multi = e._price_matrix(df)
    assert is_multi is True
    assert mat.shape == (2, 2)


def test_price_matrix_non_positive_price_raises():
    """非正价格抛 DataFeedError。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 0.0]}, index=_idx(2))
    with pytest.raises(DataFeedError, match="non-positive price"):
        e._price_matrix(df)


def test_price_matrix_all_nan_raises():
    """全 NaN 价格矩阵抛 DataFeedError。"""
    e = BacktestEngine()
    df = pd.DataFrame({"close": [float("nan"), float("nan")]}, index=_idx(2))
    with pytest.raises(DataFeedError):
        e._price_matrix(df)


def test_price_matrix_no_price_column_raises():
    """既无 close 也无任何数值列时抛 DataFeedError。"""
    e = BacktestEngine()
    df = pd.DataFrame({"note": ["a", "b"]}, index=_idx(2))
    with pytest.raises(DataFeedError):
        e._price_matrix(df)


# ---------------------------------------------------------------- run 入口


def test_run_with_asset_columns_without_close():
    """多资产路径允许无 close 列。"""
    df = pd.DataFrame({"A": [10.0, 11.0, 12.0], "B": [20.0, 21.0, 22.0]}, index=_idx(3))
    res = BacktestEngine().run(df, weights=[0.5, 0.5], allow_synthetic=True)
    assert res["positions"].shape[1] == 2


def test_run_close_column_all_nan_raises():
    """单列 close 全 NaN 时拒绝（DataFeedError，fail-closed 不造合成价）。"""
    df = pd.DataFrame({"close": [float("nan")] * 3}, index=_idx(3))
    with pytest.raises(DataFeedError, match="no valid numeric data"):
        BacktestEngine().run(df, allow_synthetic=True)


def test_run_rejects_non_numeric_costs_string():
    """costs 为不可解析字符串时抛 ValueError。"""
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    with pytest.raises(ValueError, match="costs must be a finite non-negative number"):
        BacktestEngine().run(df, costs="abc", allow_synthetic=True)


def test_run_costs_none_is_zero():
    """costs=None 按 0 处理。"""
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    res = BacktestEngine().run(df, costs=None, allow_synthetic=True)
    assert res["costs"] == 0.0


# ---------------------------------------------------------------- 换手率


def test_compute_turnover_rate_first_day_filled():
    """首日换手按 ‖w‖₁/total_weight 补齐（不硬编码 1.0）。"""
    e = BacktestEngine()
    idx = _idx(3)
    pos = pd.DataFrame({"position": [0.0, 0.5, 1.0]}, index=idx)
    gross = pd.Series([1.0, 1.0, 1.0], index=idx)
    out = e._compute_turnover_rate(pos, gross, np.array([1.0]), 1.0, leverage=1.0)
    assert out.iloc[0] == pytest.approx(1.0)


def test_compute_turnover_rate_zero_weight_is_zero():
    """空仓（零权重）时首日换手为 0（不扣费）。"""
    e = BacktestEngine()
    idx = _idx(3)
    pos = pd.DataFrame({"position": [0.0, 0.0, 0.0]}, index=idx)
    gross = pd.Series([1.0, 1.0, 1.0], index=idx)
    out = e._compute_turnover_rate(pos, gross, np.array([0.0]), 1.0, leverage=0.0)
    assert out.iloc[0] == pytest.approx(0.0)


# ---------------------------------------------------------------- 回撤渲染


def test_drawdown_episodes_with_non_timestamp_index():
    """非 Timestamp 索引也能渲染回撤表格。"""
    e = BacktestEngine()
    s = pd.Series([100.0, 80.0, 120.0], index=[0, 1, 2])
    html = e._drawdown_episodes_html(s)
    assert "<table" in html


def test_drawdown_episodes_sorts_by_depth():
    """回撤区间按深度排序（最深的在前）。"""
    e = BacktestEngine()
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    s = pd.Series([100.0, 90.0, 95.0, 120.0, 60.0, 130.0], index=idx)
    html = e._drawdown_episodes_html(s, top_n=2)
    assert html.count("<tr><td>") == 2
    # 最深的一行排在前面（-50.00% 先于 -10.00%）
    assert html.index("-50.00%") < html.index("-10.00%")


# ---------------------------------------------------------------- 信号


def test_signal_sma_crossover_without_close_column_uses_first_column():
    """无 close 列时用首列计算交叉信号（不抛）。"""
    df = pd.DataFrame({"px": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, index=_idx(6))
    w = Signal(method="sma_crossover", window_short=2, window_long=4).generate(df)
    assert len(w) == 1
    assert w[0] == pytest.approx(1.0)


def test_generate_signal_with_empty_static_weights_falls_back():
    """静态权重为空数组时回落到信号模型。"""
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=_idx(3))
    w = generate_signal(df, method="equal_weight", weights=[])
    assert len(w) == 1
    assert w[0] == pytest.approx(1.0)


def test_generate_signal_with_nan_static_weights_become_zero():
    """静态权重含 NaN 时归零（不产生 NaN 收益）。"""
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=_idx(3))
    w = generate_signal(df, weights=[float("nan")])
    assert list(w) == [pytest.approx(0.0)]
