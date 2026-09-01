import pandas as pd
import pytest
import numpy as np


def test_engine_event_pit():
    from hero_quant.backtest.engine import BacktestEngine
    from hero_quant.backtest.validation import ValidationError

    prices = pd.DataFrame(
        {"close": [100, 101, 102, 101, 103]},
        index=pd.date_range("2026-08-01", periods=5),
    )
    weights = [0.5, 0.5]
    # weights_on > price_date must raise ValidationError (PIT violation — uses future data)
    with pytest.raises(ValidationError):
        BacktestEngine().run(
            prices, weights, costs=0.001, weights_on="2026-08-05", price_date="2026-08-01"
        )

    # sanity: valid PIT should NOT raise
    res = BacktestEngine().run(
        prices, weights, costs=0.001, weights_on="2026-07-30", price_date="2026-08-01", allow_synthetic=True
    )
    assert "equity" in res


def test_engine_event_loop_methods():
    """Engine must expose event-driven API: on_bar loop + historical_base_price + _align + _execute_bars."""
    from hero_quant.backtest.engine import BacktestEngine

    e = BacktestEngine()
    # event-driven surface
    assert hasattr(e, "on_bar"), "BacktestEngine missing on_bar"
    assert hasattr(e, "_align"), "BacktestEngine missing _align"
    assert hasattr(e, "_execute_bars"), "BacktestEngine missing _execute_bars"
    # historical_base_price may be attribute or property
    assert hasattr(e, "historical_base_price")


def test_engine_align_and_execute():
    from hero_quant.backtest.engine import BacktestEngine

    e = BacktestEngine(initial_capital=100.0)
    prices = pd.DataFrame(
        {"close": [100, 101, 102], "open": [99, 100.5, 101.5]},
        index=pd.date_range("2026-08-01", periods=3),
    )
    # _align should return next-day open when available
    aligned = e._align(prices, 0)
    assert aligned == pytest.approx(100.5)

    # _execute_bars capital pre-check proportional scaling
    target = pd.Series([80.0, 80.0], index=["asset_0", "asset_1"])  # sum 160 > capital 100
    scaled = e._execute_bars(target, available_capital=100.0)
    # proportional scaling: sum <= capital
    assert float(scaled.sum()) == pytest.approx(100.0, rel=1e-6)
    # ratios preserved
    assert float(scaled.iloc[0] / scaled.sum()) == pytest.approx(0.5, rel=1e-6)

    # within capital: no scaling
    target2 = pd.Series([30.0, 20.0])
    scaled2 = e._execute_bars(target2, available_capital=100.0)
    assert float(scaled2.sum()) == pytest.approx(50.0)


def test_engine_zero_capital_guard():
    """M3: initial_capital must be >0 and finite — guard at __init__ and run."""
    from hero_quant.backtest.engine import BacktestEngine

    # __init__ guard
    with pytest.raises(ValueError):
        BacktestEngine(initial_capital=0)
    with pytest.raises(ValueError):
        BacktestEngine(initial_capital=-1)
    with pytest.raises(ValueError):
        BacktestEngine(initial_capital=float("nan"))
    with pytest.raises(ValueError):
        BacktestEngine(initial_capital=float("inf"))

    # run entry guard — mutated capital
    import pandas as pd

    e = BacktestEngine(initial_capital=100.0)
    e.initial_capital = 0  # simulate mis-config after construction
    prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValueError):
        e.run(prices, allow_synthetic=True)


def test_engine_invalid_date_raises():
    """M2: invalid date format must raise ValidationError, not be silently swallowed."""
    from hero_quant.backtest.engine import BacktestEngine
    from hero_quant.backtest.validation import ValidationError
    import pandas as pd

    prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValidationError):
        BacktestEngine().run(prices, weights_on="not-a-date", price_date="2026-08-01")
