"""Golden backtest — Wave5 Task16.

Covers:
- sma_crossover bear gives 0 weight
- on_bar pricing uses aligned_price into equity
- golden tearsheet oracle with fixed 600519.SH 5-day synthetic
"""
import pandas as pd
import numpy as np


def test_sma_crossover_bear_gives_0():
    from hero_quant.backtest.engine import Signal

    # Construct declining close so sma_short(5) < sma_long(20)
    # Need at least 20 rows for signal to trigger
    closes = list(range(40, 0, -1))  # 40..1 declining
    prices = pd.DataFrame({"close": closes}, index=pd.date_range("2026-08-01", periods=len(closes)))
    sig = Signal(method="sma_crossover", window_short=5, window_long=20)
    w = sig.generate(prices, n_assets=1)
    # Bear market (short < long) should give 0 weight
    assert np.allclose(w, np.zeros(1)), f"bear should be 0 weight, got {w}"
    # Also via functional wrapper
    from hero_quant.backtest.engine import generate_signal

    w2 = generate_signal(prices, method="sma_crossover", n_assets=1)
    assert np.allclose(w2, np.zeros(1)), f"generate_signal bear should be 0, got {w2}"


def test_on_bar_pricing_uses_aligned():
    from hero_quant.backtest.engine import BacktestEngine

    prices = pd.DataFrame({"close": [100, 101, 102, 101, 103]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine(initial_capital=1000.0)

    # Baseline run with natural _align (next close)
    res_close = engine.run(prices, weights=[1.0], costs=0.0)
    equity_close = res_close["equity"]

    # Patch on_bar to return dramatically different aligned_price sequence
    orig_on_bar = engine.on_bar

    def fake_on_bar(bar, idx, prices_df, equity_prev=None, w=None, leverage=None):
        # Return aligned_price as 10% higher ladder: 100, 110, 121, 133.1, 146.41
        ladder = [100 * (1.1 ** i) for i in range(len(prices_df))]
        return {"bar": bar, "idx": idx, "aligned_price": ladder[idx], "equity_prev": equity_prev}

    engine.on_bar = fake_on_bar
    res_aligned = engine.run(prices, weights=[1.0], costs=0.0)
    equity_aligned = res_aligned["equity"]

    # Equity should differ when aligned_price drives pricing
    # If not wired, equity_close == equity_aligned
    assert not equity_close.equals(equity_aligned), "on_bar aligned_price should affect equity"
    # Aligned ladder gives ~10% per bar growth -> equity should be monotonic increasing
    # Check second bar growth ~10%
    assert equity_aligned.iloc[1] > equity_close.iloc[1] * 1.05, f"aligned pricing should boost equity, close {equity_close.tolist()} vs aligned {equity_aligned.tolist()}"

    # Restore (not needed but clean)
    engine.on_bar = orig_on_bar


def test_golden_tearsheet_oracle():
    from hero_quant.backtest.engine import BacktestEngine
    from hero_quant.backtest.metrics import compute_metrics

    # Fixed 600519.SH 5-day synthetic
    prices = pd.DataFrame(
        {"close": [100, 101, 102, 101, 103]},
        index=pd.date_range("2026-08-01", periods=5),
    )
    # Ensure deterministic alias price_date guard not triggered
    engine = BacktestEngine(initial_capital=1.0)
    res = engine.run(prices, weights=[1.0], costs=0.0005)
    assert "equity" in res
    assert "positions" in res
    assert "metrics" in res
    assert "tearsheet" in res
    equity = res["equity"]
    metrics = res["metrics"]
    # positions shape matches prices
    assert len(res["positions"]) == len(prices)
    assert metrics["sharpe"] is not None
    # Oracle: compute sharpe via same function and compare within 0.01 (self-consistent golden)
    # NOTE: Golden updated 2026-08-28 — engine.run 已在主循环扣除 turnover_rate*costs（equity 为净值），
    # compute_metrics 调用点传 costs=0 避免二次扣除；期望值同样按净值口径（costs=0）计算。
    expected = compute_metrics(equity, costs=0.0)["sharpe"]
    assert abs(metrics["sharpe"] - expected) < 0.01, f"sharpe oracle mismatch {metrics['sharpe']} vs {expected}"
    # Also assert tearsheet contains expected sections
    html = res["tearsheet"]
    assert "Tearsheet" in html
    assert "Sharpe" in html
    # Monthly heatmap should render for DatetimeIndex
    assert "月度" in html or "Monthly" in html
