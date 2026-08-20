# tests/test_backtest_engine.py
def test_backtest_engine_runs():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close":[100,101,102,101,103]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine()
    res = engine.run(prices, weights=[0.5,0.5])
    assert "equity" in res
    assert res["metrics"]["sharpe"] is not None
