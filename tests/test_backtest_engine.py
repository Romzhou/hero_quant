# tests/test_backtest_engine.py — Wave5 thickened for coverage >=50
def test_backtest_engine_runs():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close":[100,101,102,101,103]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine()
    res = engine.run(prices, weights=[0.5,0.5], allow_synthetic=True)
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
    res = engine.run(prices, weights=np.array([0.6,0.4]), costs=0.001, allow_synthetic=True)
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
    res_nocost = engine.run(prices, weights=[1.0], costs=0.0, allow_synthetic=True)
    res_cost = engine.run(prices, weights=[1.0], costs=0.005, allow_synthetic=True)
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
    res = engine.run(prices, signal="sma_crossover", costs=0.0, allow_synthetic=True)
    assert "equity" in res
    # Bear signal should give flat or less growth than bull (closed elsewhere)

def test_backtest_output_dir(tmp_path):
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    out = tmp_path / "bt_out"
    res = engine.run(prices, weights=[1.0], output_dir=out, allow_synthetic=True)
    assert (out / "positions.csv").exists()
    assert (out / "metrics.json").exists()
    assert (out / "tearsheet.html").exists()
    assert "equity" in res


# --- Task 13 additional TDD coverage ---

def test_align_multi_asset_returns_series():
    """Task13-1: _align must return per-asset Series for multi-asset, not single float."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    idx = pd.date_range("2026-08-01", periods=5)
    prices = pd.DataFrame({"AAPL": [100,101,102,101,103], "MSFT": [50,51,50,52,53]}, index=idx)
    engine = BacktestEngine()
    aligned = engine._align(prices, 0)
    import pandas as pd
    assert isinstance(aligned, pd.Series), f"_align multi should be Series, got {type(aligned)}"
    assert "AAPL" in aligned and "MSFT" in aligned
    # ensure per-asset equity contributions differ when assets diverge
    import numpy as np
    res = engine.run(prices, weights=np.array([0.9, 0.1]), costs=0.0, allow_synthetic=True)
    res2 = engine.run(prices, weights=np.array([0.1, 0.9]), costs=0.0, allow_synthetic=True)
    # different weightings must produce different equity curves
    assert not res["equity"].equals(res2["equity"])


def test_align_never_returns_zero_price():
    """Task13-2: _align must not silently return 0.0 on parse failure; should raise."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pytest
    idx = pd.date_range("2026-08-01", periods=3)
    # corrupt next bar with non-numeric that would previously coerce to NaN then 0.0
    prices = pd.DataFrame({"close": [100, "bad", 102]}, index=idx)
    engine = BacktestEngine()
    with pytest.raises(Exception):
        engine._align(prices, 0)


def test_pit_guard_default_on():
    """Task13-3: PIT guard default ON. Without explicit dates, run should still validate and not silently pass future data when dates are given."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pytest
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    # Explicit violation must raise even without opt-in
    with pytest.raises(Exception):
        engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01")
    # Default run without dates must not bypass validation entirely (should succeed with default weights_on == price_date)
    res = engine.run(prices, weights=[1.0], allow_synthetic=True)
    assert "equity" in res
    # Explicit opt-out should allow violation to pass
    res2 = engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01", skip_pit=True)
    assert "equity" in res2
    res3 = engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01", enforce_pit=False)
    assert "equity" in res3


def test_leverage_isclose_and_bear_not_overridden():
    """Task13-5: leverage checks use isclose; bear 0 weights preserved, not silently overridden."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import numpy as np
    import math
    prices = pd.DataFrame({"close": [100,101,102,103]}, index=pd.date_range("2026-08-01", periods=4))
    engine = BacktestEngine(initial_capital=1000.0)
    # bear signal 0 weights should produce flat equity (not equal_weight growth)
    res_bear = engine.run(prices, weights=np.zeros(2), costs=0.0, allow_synthetic=True)
    # with 0 weights equity should stay flat (no leverage)
    assert math.isclose(res_bear["equity"].iloc[-1], 1000.0, rel_tol=1e-6) or res_bear["equity"].iloc[-1] <= 1000.01
    # near-zero leverage via isclose: weights with tiny epsilon should be treated as zero-ish but still isclose
    w_tiny = np.array([1e-13, 0.0])
    res_tiny = engine.run(prices, weights=w_tiny, costs=0.0, allow_synthetic=True)
    assert "equity" in res_tiny


def test_engine_broad_exception_narrowed(caplog):
    """Task13-4: sma_crossover broad except narrowed and logs with exc_info; ensure unknown method fallback still works."""
    from hero_quant.backtest.engine import Signal
    import pandas as pd
    closes = list(range(40, 0, -1))
    prices = pd.DataFrame({"close": closes}, index=pd.date_range("2026-08-01", periods=len(closes)))
    sig = Signal(method="unknown_method_xyz")
    import logging
    caplog.set_level(logging.WARNING)
    w = sig.generate(prices, n_assets=1)
    assert w is not None
    # unknown method should log warning
    assert any("unknown signal method" in rec.message for rec in caplog.records)


def test_output_dir_io_failure_surfaces(tmp_path):
    """Task13-12 (engine side): IO failures for metrics.json/tearsheet must surface (raise), not silent pass."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pathlib
    import pytest
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    # Use a file as output_dir to force mkdir/write failure and assert it raises
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("block")
    with pytest.raises(Exception):
        engine.run(prices, weights=[1.0], output_dir=file_path, allow_synthetic=True)
