"""W4 覆盖率补齐：backtest/metrics.py 未覆盖分支。

覆盖目标（src/hero_quant/backtest/metrics.py）：
- sharpe_ratio: None / 长度不足 / 全 NaN 收益 / 零波动 / 正常年化
- max_drawdown: None / 空 / DataFrame / list 输入 / cummax==0 边界
- annual_return: None / 长度不足 / DataFrame / list / 起点为 0 / 负比例
- turnover: DataFrame 持仓 / Series 持仓 / weights 回落 / 含 NaN·Inf 权重的 fail-closed
- compute_metrics: 空序列零指标 / DataFrame(equity 列) / costs 扣费路径 / 破产截断

断言均为具体数值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from hero_quant.backtest.metrics import (
    annual_return,
    compute_metrics,
    max_drawdown,
    sharpe_ratio,
    turnover,
)


# ---------------------------------------------------------------- sharpe_ratio


def test_sharpe_none_and_short_series_is_zero():
    """None 与长度不足 2 的序列回落 0.0。"""
    assert sharpe_ratio(None) == 0.0
    assert sharpe_ratio(pd.Series([1.0])) == 0.0


def test_sharpe_all_nan_returns_is_zero():
    """收益全 NaN（无有效变动）时回落 0.0。"""
    assert sharpe_ratio(pd.Series([float("nan"), float("nan"), float("nan")])) == 0.0


def test_sharpe_zero_volatility_is_zero():
    """零波动（常数序列）无法定义 Sharpe，回落 0.0。"""
    assert sharpe_ratio(pd.Series([100.0] * 10)) == 0.0


def test_sharpe_positive_for_trending_equity():
    """单调上涨且有波动的序列 Sharpe > 0。"""
    s = pd.Series([100.0, 101.0, 99.0, 103.0, 105.0, 104.0, 108.0, 110.0])
    assert sharpe_ratio(s) > 0.0


def test_sharpe_negative_for_falling_equity():
    """单调下跌序列 Sharpe < 0。"""
    s = pd.Series([110.0, 108.0, 104.0, 103.0, 99.0, 101.0, 100.0, 98.0])
    assert sharpe_ratio(s) < 0.0


def test_sharpe_sanitizes_infinite_pct_change():
    """权益触 0 后回升产生 inf pct_change，Sharpe 不得为 inf/NaN。"""
    s = pd.Series([100.0, 0.0, 50.0, 60.0])
    out = sharpe_ratio(s)
    assert math.isfinite(out)


# ---------------------------------------------------------------- max_drawdown


def test_max_drawdown_none_and_empty_is_zero():
    """None 与空序列回撤为 0.0。"""
    assert max_drawdown(None) == 0.0
    assert max_drawdown(pd.Series(dtype=float)) == 0.0


def test_max_drawdown_accepts_dataframe_first_column():
    """DataFrame 输入取首列。"""
    df = pd.DataFrame({"x": [100.0, 80.0, 90.0]})
    assert max_drawdown(df) == pytest.approx(-0.2)


def test_max_drawdown_accepts_plain_list():
    """普通 list 输入归一为 Series 后计算。"""
    assert max_drawdown([100.0, 90.0, 110.0]) == pytest.approx(-0.1)


def test_max_drawdown_zero_peak_no_nan():
    """cummax==0 处回撤定义为 0（不产生 NaN），其余真实回撤仍如实给出。"""
    out = max_drawdown(pd.Series([0.0, 10.0, 5.0]))
    assert out == pytest.approx(-0.5)
    assert not math.isnan(out)


# ---------------------------------------------------------------- annual_return


def test_annual_return_none_and_short_is_zero():
    """None 与长度不足 2 回落 0.0。"""
    assert annual_return(None) == 0.0
    assert annual_return(pd.Series([1.0])) == 0.0


def test_annual_return_cagr_value():
    """CAGR = (end/start)^(252/n) - 1，n 为观测跨度。"""
    s = pd.Series([100.0, 110.0])  # n = 1
    expected = 1.1 ** (252 / 1) - 1
    assert annual_return(s) == pytest.approx(expected, rel=1e-9)


def test_annual_return_zero_start_is_zero():
    """起点为 0 无法定义 CAGR，回落 0.0。"""
    assert annual_return(pd.Series([0.0, 10.0, 20.0])) == 0.0


def test_annual_return_negative_ratio_is_zero():
    """终点为负（比例 <= 0）回落 0.0，不返回复数。"""
    assert annual_return(pd.Series([100.0, -50.0])) == 0.0


def test_annual_return_accepts_dataframe_and_list():
    """DataFrame / list 输入均被接受并给出同一结果。"""
    df = pd.DataFrame({"equity": [100.0, 120.0, 150.0]})
    a = annual_return(df)
    b = annual_return([100.0, 120.0, 150.0])
    assert a == pytest.approx(b, rel=1e-12)
    assert a > 0.0


def test_annual_return_non_numeric_coerced_to_zero_when_empty():
    """全脏数据（coerce 后为空）回落 0.0。"""
    assert annual_return(pd.Series(["a", "b"], dtype=object)) == 0.0


# ---------------------------------------------------------------- turnover


def test_turnover_dataframe_positions_single_side_mean():
    """DataFrame 持仓：逐标的绝对变动求和后取单边日均。"""
    pos = pd.DataFrame({"a": [1.0, 2.0, 3.0]})
    assert turnover(pos) == pytest.approx(0.5)


def test_turnover_series_positions():
    """Series 持仓：差分绝对值均值除以 2（单边口径）。"""
    assert turnover(pd.Series([1.0, 2.0, 3.0])) == pytest.approx(0.5)


def test_turnover_weights_fallback_matches_positions_path():
    """weights 回落口径与 positions 路径一致（日均单边）。"""
    w = [0.0, 1.0, 0.0, 1.0, 0.0]
    assert turnover(None, w) == pytest.approx(0.5)


def test_turnover_weights_with_nan_is_zero():
    """含 NaN 的权重视为不可信，fail-closed 回落 0.0（不清洗后计算）。"""
    assert turnover(None, [0.0, float("nan"), 1.0]) == 0.0


def test_turnover_weights_with_inf_is_zero():
    """含 Inf 的权重 fail-closed 回落 0.0。"""
    assert turnover(None, [0.0, float("inf"), 1.0]) == 0.0


def test_turnover_single_weight_is_zero():
    """只有一个权重时无变动可算，回落 0.0。"""
    assert turnover(None, [1.0]) == 0.0


def test_turnover_no_input_is_zero():
    """无持仓无权重时回落 0.0。"""
    assert turnover() == 0.0


# ---------------------------------------------------------------- compute_metrics


def test_compute_metrics_empty_series_returns_zero_block():
    """空序列返回全零指标字典（键齐全）。"""
    out = compute_metrics(pd.Series(dtype=float))
    assert set(out) == {"sharpe", "annual_return", "max_drawdown", "turnover", "volatility", "cumulative_return"}
    assert all(v == 0.0 for v in out.values())


def test_compute_metrics_dataframe_prefers_equity_column():
    """DataFrame 含 equity 列时优先取 equity。"""
    df = pd.DataFrame({"equity": [100.0, 110.0, 121.0], "other": [0.0, 0.0, 0.0]})
    out = compute_metrics(df)
    assert out["cumulative_return"] == pytest.approx(0.21, abs=1e-12)


def test_compute_metrics_dataframe_without_equity_takes_first_column():
    """无 equity 列时取首列。"""
    df = pd.DataFrame({"x": [100.0, 110.0, 121.0]})
    out = compute_metrics(df)
    assert out["cumulative_return"] == pytest.approx(0.21, abs=1e-12)


def test_compute_metrics_costs_drag_is_subtractive():
    """costs 为每 Bar 固定扣费（additive），累计收益被拉低。"""
    equity = pd.Series([100.0, 110.0, 121.0])
    gross = compute_metrics(equity, costs=0.0)
    net = compute_metrics(equity, costs=0.01)
    assert gross["cumulative_return"] == pytest.approx(0.21, abs=1e-12)
    # gross_ret=[0,0.1,0.1] -> net_ret=[0,0.09,0.09] -> 净权益 [100,109,118.81]
    assert net["cumulative_return"] == pytest.approx(0.1881, abs=1e-12)
    assert net["cumulative_return"] < gross["cumulative_return"]


def test_compute_metrics_costs_bankrupt_bar_is_floored():
    """成本导致某 Bar 破产时权益按有限责任截断为 0，不产生负权益/复数。"""
    equity = pd.Series([100.0, 90.0, 95.0])
    out = compute_metrics(equity, costs=0.5)
    assert math.isfinite(out["cumulative_return"])
    assert out["max_drawdown"] <= 0.0


def test_compute_metrics_invalid_costs_falls_back_to_zero():
    """costs 不可解析时按 0 处理（不抛错）。"""
    out = compute_metrics(pd.Series([100.0, 110.0]), costs="abc")
    assert out["cumulative_return"] == pytest.approx(0.1, abs=1e-12)


def test_compute_metrics_uses_positions_for_turnover():
    """positions 传入时换手率非 0。"""
    equity = pd.Series([100.0, 110.0, 121.0])
    pos = pd.DataFrame({"a": [1.0, 2.0, 3.0]})
    out = compute_metrics(equity, positions=pos)
    assert out["turnover"] == pytest.approx(0.5)


def test_compute_metrics_volatility_annualized():
    """波动率按 252 交易日年化。"""
    equity = pd.Series([100.0, 102.0, 101.0, 104.0, 103.0, 107.0])
    out = compute_metrics(equity)
    rets = equity.pct_change().dropna()
    expected = float(rets.std(ddof=1) * math.sqrt(252))
    assert out["volatility"] == pytest.approx(expected, rel=1e-12)
