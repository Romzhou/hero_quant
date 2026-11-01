"""tests/test_pr234_realism.py — PR-234 回测现实性（滑点/涨跌停/停牌/退市/基准）。

默认行为回归基线来源：master b37265c 未改动的 BacktestEngine.run（本 PR 只新增 realism.py，
engine.py 未触碰）；黄金值由该提交下的引擎在同一条合成行情上实测导出，浮点容差 1e-9。
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from hero_quant.backtest import realism
from hero_quant.backtest.engine import BacktestEngine
from hero_quant.backtest.metrics import max_drawdown
from hero_quant.backtest.realism import (
    TradingConstraints,
    TradingCosts,
    apply_price_limit,
    apply_slippage,
    buy_and_hold,
    delisted_after,
    is_halted,
    realism_report,
)

# ------------------------------------------------------------------ 成本与约束口径


def test_trading_costs_default_is_bps_and_frozen():
    costs = TradingCosts()
    assert (costs.commission_bps, costs.slippage_bps, costs.stamp_duty_bps) == (3.0, 2.0, 5.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        costs.commission_bps = 1.0  # type: ignore[misc]
    with pytest.raises(ValueError):
        TradingCosts(commission_bps=-0.1)


def test_trading_constraints_none_means_unlimited_and_rejects_bad_pct():
    assert TradingConstraints().price_limit_pct is None
    with pytest.raises(ValueError):
        TradingConstraints(price_limit_pct=-0.1)
    with pytest.raises(ValueError):
        TradingConstraints(price_limit_pct=1.0)


def test_apply_slippage_direction_buy_up_sell_down():
    assert apply_slippage(100.0, "buy", bps=25.0) == pytest.approx(100.25)
    assert apply_slippage(100.0, "sell", bps=25.0) == pytest.approx(99.75)
    # 别名与大小写
    assert apply_slippage(100.0, "LONG", bps=25.0) == pytest.approx(100.25)
    assert apply_slippage(100.0, "short", bps=25.0) == pytest.approx(99.75)
    # bps=0 原价
    assert apply_slippage(37.5, "buy", bps=0.0) == 37.5


def test_apply_slippage_fails_closed_on_bad_input():
    with pytest.raises(ValueError):
        apply_slippage(100.0, "hold", bps=1.0)
    with pytest.raises(ValueError):
        apply_slippage(100.0, "buy", bps=-1.0)
    with pytest.raises(ValueError):
        apply_slippage(0.0, "buy", bps=1.0)
    with pytest.raises(ValueError):
        apply_slippage(100.0, "sell", bps=10_001.0)  # 压价到 <= 0


def test_apply_price_limit_clips_upper_lower_and_identity():
    assert apply_price_limit(100.0, 120.0, limit_pct=0.10) == pytest.approx(110.0)
    assert apply_price_limit(100.0, 80.0, limit_pct=0.10) == pytest.approx(90.0)
    assert apply_price_limit(100.0, 104.0, limit_pct=0.10) == pytest.approx(104.0)
    # limit_pct=None 原样返回（含远超区间）
    assert apply_price_limit(100.0, 1_000_000.0, limit_pct=None) == 1_000_000.0
    assert apply_price_limit(100.0, 1.0, limit_pct=None) == 1.0


def test_apply_price_limit_rejects_bad_inputs():
    with pytest.raises(ValueError):
        apply_price_limit(0.0, 10.0, limit_pct=0.1)
    with pytest.raises(ValueError):
        apply_price_limit(100.0, float("nan"), limit_pct=0.1)
    with pytest.raises(ValueError):
        apply_price_limit(100.0, 10.0, limit_pct=1.0)


# ------------------------------------------------------------------ 停牌与退市


def test_is_halted_only_zero_volume():
    assert is_halted({"volume": 0}) is True
    assert is_halted({"volume": 0.0}) is True
    assert is_halted({"volume": 100}) is False
    # 缺量不是停牌证据（由 realism_report 判 volume_missing）
    assert is_halted({}) is False
    assert is_halted({"volume": float("nan")}) is False


def test_delisted_after_detects_markers():
    bars = [
        {"trade_date": "2026-09-01", "close": 10.0, "volume": 100},
        {"trade_date": "2026-09-02", "close": 10.0, "volume": 100, "delisted": True},
    ]
    assert delisted_after(bars) == pd.Timestamp("2026-09-02").date()

    by_field = [{"trade_date": "2026-09-03", "delist_date": "2026-09-03", "volume": 0}]
    assert delisted_after(by_field) == pd.Timestamp("2026-09-03").date()

    by_status = [{"trade_date": "2026-09-04", "status": "退市", "volume": 0}]
    assert delisted_after(by_status) == pd.Timestamp("2026-09-04").date()


def test_delisted_after_unknown_without_marker():
    bars = [{"trade_date": "2026-09-01", "close": 10.0, "volume": 100}]
    assert delisted_after(bars) is None
    assert delisted_after([]) is None
    assert delisted_after(None) is None


# ------------------------------------------------------------------ 基准口径


def test_buy_and_hold_return_and_drawdown_match_metrics():
    prices = pd.Series([100.0, 110.0, 99.0, 120.0])
    bench = buy_and_hold(prices)
    assert bench["degraded"] is False and bench["reason"] is None
    assert bench["total_return"] == pytest.approx(0.20)
    expected_mdd = 99.0 / 110.0 - 1.0
    assert bench["max_drawdown"] == pytest.approx(expected_mdd)
    assert bench["max_drawdown"] == pytest.approx(max_drawdown(bench["series"]))
    assert list(bench["series"]) == pytest.approx([1.0, 1.1, 0.99, 1.2])


def test_buy_and_hold_single_point_is_not_degraded():
    bench = buy_and_hold(pd.Series([42.0]))
    assert bench["degraded"] is False
    assert bench["total_return"] == 0.0
    assert bench["max_drawdown"] == 0.0


@pytest.mark.parametrize(
    "prices,reason",
    [
        (None, "prices_missing"),
        (pd.Series(dtype="float64"), "prices_missing"),
        (pd.Series([100.0, float("nan"), 90.0]), "price_missing"),
        (pd.Series([100.0, "bad"]), "price_missing"),
        (pd.Series([100.0, 0.0]), "price_non_positive"),
        (pd.Series([100.0, float("inf")]), "price_missing"),
    ],
)
def test_buy_and_hold_degrades_without_fabricating(prices, reason):
    bench = buy_and_hold(prices)
    assert bench["degraded"] is True
    assert bench["reason"] == reason
    assert bench["total_return"] is None
    assert bench["max_drawdown"] is None
    assert bench["series"] is None


# ------------------------------------------------------------------ realism_report


def _bars(*, volume=100, **overrides):
    base = {"trade_date": "2026-09-01", "close": 100.0, "volume": volume}
    base.update(overrides)
    return [base]


def test_realism_report_cost_split_uses_bps_and_sell_side_stamp_duty():
    bars = [
        {"trade_date": "2026-09-01", "close": 100.0, "volume": 1000},
        {"trade_date": "2026-09-02", "close": 110.0, "volume": 1000},
    ]
    fills = [
        {"price": 100.0, "qty": 10, "side": "buy", "trade_date": "2026-09-01"},
        {"price": 110.0, "qty": 10, "side": "sell", "trade_date": "2026-09-02"},
    ]
    report = realism_report(fills=fills, bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is False and report["reason"] is None
    assert report["trades"] == 2
    assert report["notional"] == pytest.approx(2100.0)
    assert report["commission"] == pytest.approx(2100.0 * 3 / 10_000)
    assert report["slippage"] == pytest.approx(2100.0 * 2 / 10_000)
    assert report["stamp_duty"] == pytest.approx(1100.0 * 5 / 10_000)  # 仅卖出
    assert report["total_cost"] == pytest.approx(report["commission"] + report["slippage"] + report["stamp_duty"])
    assert report["cost_bps"] == pytest.approx(report["total_cost"] / 2100.0 * 10_000)
    assert report["halted_bars"] == 0
    assert report["clipped_fills"] == 0


def test_realism_report_no_fills_is_genuine_zero_not_degraded():
    report = realism_report(fills=[], bars=_bars(), costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is False
    assert report["trades"] == 0
    assert report["notional"] == 0.0
    assert report["total_cost"] == 0.0
    assert report["cost_bps"] == 0.0


def test_realism_report_clips_fills_to_price_limit():
    fills = [{"price": 130.0, "qty": 10, "side": "buy", "prev_close": 100.0, "trade_date": "2026-09-02"}]
    report = realism_report(
        fills=fills,
        bars=_bars(),
        costs=TradingCosts(),
        constraints=TradingConstraints(price_limit_pct=0.10),
    )
    assert report["degraded"] is False
    assert report["clipped_fills"] == 1
    assert report["notional"] == pytest.approx(110.0 * 10)  # 按限价成交，不用 130
    assert report["commission"] == pytest.approx(1100.0 * 3 / 10_000)


def test_realism_report_matches_inside_limit_without_clipping():
    fills = [{"price": 105.0, "qty": 10, "side": "buy", "prev_close": 100.0}]
    report = realism_report(
        fills=fills,
        bars=_bars(),
        costs=TradingCosts(),
        constraints=TradingConstraints(price_limit_pct=0.10),
    )
    assert report["clipped_fills"] == 0
    assert report["notional"] == pytest.approx(1050.0)


def test_realism_report_derives_prev_close_from_previous_bar():
    bars = [
        {"symbol": "AAA", "trade_date": "2026-09-01", "close": 50.0, "volume": 100},
        {"symbol": "AAA", "trade_date": "2026-09-02", "close": 55.0, "volume": 100},
    ]
    fills = [{"symbol": "AAA", "trade_date": "2026-09-03", "price": 70.0, "qty": 1, "side": "buy"}]
    report = realism_report(
        fills=fills,
        bars=bars,
        costs=TradingCosts(),
        constraints=TradingConstraints(price_limit_pct=0.10),
    )
    assert report["degraded"] is False
    assert report["clipped_fills"] == 1
    assert report["notional"] == pytest.approx(55.0 * 1.10)  # 前一交易日收盘 55 -> 上限 60.5


def test_realism_report_prev_close_missing_when_limit_requires_it():
    bars = [{"trade_date": "2026-09-01", "close": 100.0, "volume": 100}]  # 无更早 bar
    fills = [{"trade_date": "2026-09-01", "price": 100.0, "qty": 1, "side": "buy"}]
    report = realism_report(
        fills=fills,
        bars=bars,
        costs=TradingCosts(),
        constraints=TradingConstraints(price_limit_pct=0.10),
    )
    assert report["degraded"] is True
    assert report["reason"] == "prev_close_missing"
    for key in ("trades", "notional", "total_cost", "cost_bps", "halted_bars", "clipped_fills"):
        assert report[key] is None


def test_realism_report_volume_missing_degrades():
    bars = [{"trade_date": "2026-09-01", "close": 100.0}]
    report = realism_report(fills=[], bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is True
    assert report["reason"] == "volume_missing"
    assert report["notional"] is None


def test_realism_report_all_halted_degrades():
    bars = [
        {"trade_date": "2026-09-01", "close": 100.0, "volume": 0},
        {"trade_date": "2026-09-02", "close": 100.0, "volume": 0},
    ]
    report = realism_report(fills=[], bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is True
    assert report["reason"] == "all_halted"
    assert report["halted_bars"] is None


def test_realism_report_trailing_halt_without_marker_is_delisted_unknown():
    bars = [
        {"trade_date": "2026-09-01", "close": 100.0, "volume": 100},
        {"trade_date": "2026-09-02", "close": 100.0, "volume": 0},
    ]
    report = realism_report(fills=[], bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is True
    assert report["reason"] == "delisted_unknown"


def test_realism_report_trailing_halt_with_marker_reports_delist_date():
    bars = [
        {"trade_date": "2026-09-01", "close": 100.0, "volume": 100},
        {"trade_date": "2026-09-02", "close": 100.0, "volume": 0, "delisted": True},
    ]
    report = realism_report(fills=[], bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is False
    assert report["delisted_at"] == "2026-09-02"
    assert report["halted_bars"] == 1


def test_realism_report_bars_missing_and_fills_missing():
    missing_bars = realism_report(fills=[], bars=[], costs=TradingCosts(), constraints=TradingConstraints())
    assert (missing_bars["degraded"], missing_bars["reason"]) == (True, "bars_missing")

    missing_fills = realism_report(fills=None, bars=_bars(), costs=TradingCosts(), constraints=TradingConstraints())
    assert (missing_fills["degraded"], missing_fills["reason"]) == (True, "fills_missing")


def test_realism_report_invalid_fills_degrade():
    bars = _bars()
    for fills in ([{"qty": 1, "side": "buy"}], [{"price": float("nan"), "qty": 1, "side": "buy"}], [["price"]]):
        report = realism_report(fills=fills, bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
        assert report["degraded"] is True
        assert report["reason"] == "fills_invalid"


def test_realism_report_accepts_dataframe_inputs():
    bars = pd.DataFrame(
        {"trade_date": ["2026-09-01"], "close": [100.0], "volume": [1000], "symbol": ["AAA"]}
    )
    fills = pd.DataFrame(
        {"price": [100.0], "qty": [10.0], "side": ["sell"], "prev_close": [99.0], "symbol": ["AAA"]}
    )
    report = realism_report(
        fills=fills,
        bars=bars,
        costs=TradingCosts(),
        constraints=TradingConstraints(price_limit_pct=0.10),
    )
    assert report["degraded"] is False
    assert report["trades"] == 1
    assert report["stamp_duty"] > 0.0
    assert report["clipped_fills"] == 0


def test_realism_report_single_bar_does_not_degrade():
    bars = [{"trade_date": "2026-09-01", "close": 100.0, "volume": 500}]
    fills = [{"price": 100.0, "qty": 1, "side": "buy", "prev_close": 100.0}]
    report = realism_report(fills=fills, bars=bars, costs=TradingCosts(), constraints=TradingConstraints())
    assert report["degraded"] is False
    assert report["trades"] == 1


def _assert_no_nan(value):
    if isinstance(value, float):
        assert not math.isnan(value)
    elif isinstance(value, dict):
        for item in value.values():
            _assert_no_nan(item)


@pytest.mark.parametrize(
    "bars,fills,constraints",
    [
        ([{"trade_date": "2026-09-01", "close": 100.0, "volume": 100}],
         [{"price": 100.0, "qty": 1, "side": "buy"}], TradingConstraints(price_limit_pct=0.10)),
        ([{"trade_date": "2026-09-01", "close": float("nan"), "volume": 100}],
         [{"price": 100.0, "qty": 1, "side": "buy"}], TradingConstraints(price_limit_pct=0.10)),
        ([{"trade_date": "2026-09-01", "close": 100.0, "volume": float("nan")}],
         [{"price": 100.0, "qty": 1, "side": "buy"}], TradingConstraints()),
    ],
)
def test_realism_report_never_returns_nan(bars, fills, constraints):
    report = realism_report(fills=fills, bars=bars, costs=TradingCosts(), constraints=constraints)
    _assert_no_nan(report)


def test_realism_report_type_guards():
    with pytest.raises(TypeError):
        realism_report(fills=[], bars=_bars(), costs={"commission_bps": 1}, constraints=TradingConstraints())
    with pytest.raises(TypeError):
        realism_report(fills=[], bars=_bars(), costs=TradingCosts(), constraints={"price_limit_pct": 0.1})


# ------------------------------------------------ 默认行为零变化（回归黄金值）

_GOLDEN_EQUITY = [
    0.9995,
    0.982752220443,
    1.016224386877,
    1.005375797662,
    1.029987810762,
    1.00338881866,
    1.0368546421,
    1.036837902756,
]
_GOLDEN_METRICS = {
    "sharpe": 3.51727516963,
    "annual_return": 2.744642768681,
    "max_drawdown": -0.02582456979,
    "turnover": 0.010411578799,
    "volatility": 0.395289075809,
    "cumulative_return": 0.037356581047,
}


def test_default_run_output_identical_to_pre_change_baseline():
    """engine.py 未改动：默认 costs=0.0005 的逐字段输出必须与基线一致（容差 1e-9）。"""
    prices = pd.DataFrame(
        {"close": [100.0, 101.5, 99.8, 103.2, 102.1, 104.6, 101.9, 105.3]},
        index=pd.date_range("2026-08-03", periods=8, freq="B"),
    )
    result = BacktestEngine(initial_capital=1.0).run(prices, weights=[1.0], allow_synthetic=True)

    assert set(result) == {
        "equity",
        "metrics",
        "positions",
        "fills",
        "tearsheet",
        "metrics_json",
        "engine",
        "non_pit",
        "costs",
    }
    assert result["costs"] == 0.0005
    assert result["engine"] == "default"
    assert result["non_pit"] is False
    assert len(result["equity"]) == len(_GOLDEN_EQUITY)
    for got, expected in zip(result["equity"].to_numpy(), _GOLDEN_EQUITY):
        assert abs(float(got) - expected) <= 1e-9
    for key, expected in _GOLDEN_METRICS.items():
        assert abs(float(result["metrics"][key]) - expected) <= 1e-9
    # realism 模块完全独立：import 不改变引擎的返回结构
    assert hasattr(realism, "realism_report")
    assert np.isfinite(result["equity"].to_numpy()).all()
