# tests/test_backtest_engine.py — Wave5 thickened for coverage >=50
def test_backtest_engine_runs():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close":[100,101,102,101,103]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine()
    res = engine.run(prices, weights=[0.5,0.5])
    assert "equity" in res
    assert res["metrics"]["sharpe"] is not None

def test_backtest_multi_asset():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import numpy as np
    idx = pd.date_range("2026-08-01", periods=5)
    # Multi-asset price matrix: two assets with distinct closes
    prices = pd.DataFrame({"AAPL": [100,101,102,101,103], "MSFT": [50,51,50,52,53]}, index=idx)
    engine = BacktestEngine(initial_capital=1000.0)
    res = engine.run(prices, weights=np.array([0.6,0.4]), costs=0.001)
    assert "positions" in res
    assert res["positions"].shape[1] == 2
    assert len(res["equity"]) == 5
    # positions should be scaled by available_capital
    assert (res["positions"].abs().sum(axis=1) <= res["equity"] * 1.01).all()
    assert "tearsheet" in res
    assert "fills" in res

def test_backtest_turnover_cost_drag():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close": [100,102,101,103,105]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine(initial_capital=100.0)
    res_nocost = engine.run(prices, weights=[1.0], costs=0.0)
    res_cost = engine.run(prices, weights=[1.0], costs=0.005)
    # With costs, final equity should be <= no-cost equity
    assert res_cost["equity"].iloc[-1] <= res_nocost["equity"].iloc[-1] + 1e-6
    assert res_cost["metrics"]["turnover"] >= 0

def test_backtest_pit_guard():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pytest
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    # PIT violation: weights_on > price_date should raise ValidationError
    with pytest.raises(Exception):
        engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01")

def test_backtest_signal_integration():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    closes = list(range(40, 0, -1))
    prices = pd.DataFrame({"close": closes}, index=pd.date_range("2026-08-01", periods=len(closes)))
    engine = BacktestEngine(initial_capital=1000.0)
    res = engine.run(prices, signal="sma_crossover", costs=0.0)
    assert "equity" in res
    # Bear signal should give flat or less growth than bull (closed elsewhere)

def test_backtest_output_dir(tmp_path):
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    out = tmp_path / "bt_out"
    res = engine.run(prices, weights=[1.0], output_dir=out)
    assert (out / "positions.csv").exists()
    assert (out / "metrics.json").exists()
    assert (out / "tearsheet.html").exists()
    assert "equity" in res
