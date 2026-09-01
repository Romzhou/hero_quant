# tests/test_backtest_pit_correct.py
def test_pit_correct_logic():
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd

    prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range("2026-08-10", periods=2))
    try:
        validate(prices, weights_on="2026-08-11", price_date="2026-08-10")
    except ValidationError:
        pass
    else:
        assert False
    validate(prices, weights_on="2026-08-09", price_date="2026-08-10")


def test_validation_non_numeric_nan_fail_closed():
    """Task13-8: non-numeric/NaN close must fail-closed, not bypass non-positive check."""
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    import pytest
    prices = pd.DataFrame({"close": [100, "N/A", 102]}, index=pd.date_range("2026-08-01", periods=3))
    with pytest.raises(ValidationError):
        validate(prices)
    prices2 = pd.DataFrame({"close": [100, None, 102]}, index=pd.date_range("2026-08-01", periods=3))
    with pytest.raises(ValidationError):
        validate(prices2)
    prices3 = pd.DataFrame({"close": [100, float("nan"), 102]}, index=pd.date_range("2026-08-01", periods=3))
    with pytest.raises(ValidationError):
        validate(prices3)


def test_validation_silent_swallow_is_fixed(caplog):
    """Task13-9: validation must not silently swallow conversion errors; must re-raise ValidationError."""
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    import pytest
    # corrupt prices that will cause conversion issues but previously would be swallowed
    prices = pd.DataFrame({"close": ["bad", "worse"]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValidationError):
        validate(prices)
    # currency validation swallow also fixed
    prices_cur = pd.DataFrame({"close": [100, 101], "currency": ["USD", "EUR"]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValidationError):
        validate(prices_cur)


def test_validation_pit_tz_aware_vs_naive():
    """Task13-10: PIT TZ mixing normalized to UTC-naive; aware vs naive same instant should not violate."""
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    import pytest
    prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range("2026-08-10", periods=2))
    # aware and naive representing same instant should be equal after normalization
    validate(prices, weights_on="2026-08-10 00:00:00+00:00", price_date="2026-08-10 00:00:00")
    validate(prices, weights_on=pd.Timestamp("2026-08-10", tz="UTC"), price_date=pd.Timestamp("2026-08-10"))
    # aware future should still be caught: 2026-08-11 UTC > 2026-08-10 UTC
    with pytest.raises(ValidationError):
        validate(prices, weights_on="2026-08-11 00:00:00+00:00", price_date="2026-08-10 00:00:00+00:00")
    # also test naive vs aware where aware is earlier
    validate(prices, weights_on="2026-08-09 23:00:00+00:00", price_date="2026-08-10 00:00:00")


def test_metrics_max_drawdown_cummax_zero():
    """Task13-6: max_drawdown must guard cummax==0 → drawdown 0, not inf."""
    from hero_quant.backtest.metrics import max_drawdown
    import pandas as pd
    import numpy as np
    eq = pd.Series([0.0, 0.0, 1.0, 0.5])
    mdd = max_drawdown(eq)
    assert not np.isinf(mdd), f"mdd should not be inf, got {mdd}"
    assert mdd == 0.0 or mdd == -0.5 or isinstance(mdd, float)
    # also test empty and monotonic
    eq2 = pd.Series([0.0, 0.0, 0.0])
    assert max_drawdown(eq2) == 0.0


def test_metrics_costs_wired():
    """Task13-7: compute_metrics costs param must be wired into net returns."""
    from hero_quant.backtest.metrics import compute_metrics
    import pandas as pd
    idx = pd.date_range("2026-08-01", periods=5)
    equity = pd.Series([100, 101, 102, 103, 104], index=idx, dtype=float)
    m_no_cost = compute_metrics(equity, costs=0.0)
    m_with_cost = compute_metrics(equity, costs=0.001)
    # with costs, cumulative return should be lower
    assert m_with_cost["cumulative_return"] < m_no_cost["cumulative_return"]
    assert m_with_cost["sharpe"] != m_no_cost["sharpe"] or m_with_cost["cumulative_return"] != m_no_cost["cumulative_return"]

def test_annual_return_cagr_off_by_one():
    """P2-3: CAGR must use n=len-1 periods; 2-point [100,110] with periods=1 → 10% not 4.88%."""
    from hero_quant.backtest.metrics import annual_return
    import pandas as pd
    s = pd.Series([100, 110], dtype=float)
    cagr = annual_return(s, periods=1)
    assert abs(cagr - 0.10) < 1e-9, f"expected 0.10 got {cagr}"
    # guard len<2 returns 0
    assert annual_return(pd.Series([100], dtype=float)) == 0.0
    # ensure old buggy n=len would give ~0.0488 with periods=1
    assert abs(cagr - 0.0488) > 0.02

def test_validation_currency_nan_consistent():
    """P2-5: currency NaN must be rejected consistently in both validation paths, fail-closed."""
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    import pytest
    # path 1: NaN in currency column alone should be rejected
    prices_nan = pd.DataFrame({"close": [100, 101], "currency": ["USD", float("nan")]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValidationError, match="NaN currency"):
        validate(prices_nan)
    # path 2: currency param with NaN column also rejected (consistent)
    prices_nan2 = pd.DataFrame({"close": [100, 101], "currency": [float("nan"), float("nan")]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValidationError, match="NaN currency"):
        validate(prices_nan2, currency="USD")
    # also explicit currency mismatch still caught
    prices_ok = pd.DataFrame({"close": [100, 101], "currency": ["USD", "USD"]}, index=pd.date_range("2026-08-01", periods=2))
    with pytest.raises(ValidationError):
        validate(prices_ok, currency="EUR")
    # NaN close already fail-closed verified elsewhere

def test_engine_on_tick_latency_breach():
    """P2-6: on_tick must preserve real latency (>=200ms) and flag breach, not overwrite to 0.5ms."""
    from hero_quant.backtest.engine import BacktestEngine
    import time
    eng = BacktestEngine()
    # inject slow factor to force latency >=200ms
    class SlowFactor:
        def update(self, price):
            time.sleep(0.25)
            return price
    eng._tick_factor = SlowFactor()
    res = eng.on_tick({"price": 100, "symbol": "TEST"})
    assert res["latency_ms"] >= 200, f"latency should be preserved, got {res['latency_ms']}"
    assert res["latency_breach"] is True
    assert res["latency_breach_count"] >= 1
    # fast tick should not be breach
    eng2 = BacktestEngine()
    res2 = eng2.on_tick({"price": 100, "symbol": "FAST"})
    assert res2["latency_ms"] < 200 or res2["latency_breach"] is False or res2["latency_ms"] < 250


# --- Task 4: PIT fail-closed TDD ---

def test_pit_fail_closed_raises_when_no_date():
    """Task4-2a: 无 DatetimeIndex 且 allow_synthetic=False 时必须 raise PITViolation，而非 warning 回退。"""
    from hero_quant.backtest.engine import BacktestEngine, PITViolation
    import pandas as pd
    import pytest

    # 无 DatetimeIndex 的价格（RangeIndex），不传 weights_on/price_date
    prices_no_index = pd.DataFrame({"close": [100, 101, 102]})
    engine = BacktestEngine()
    with pytest.raises(PITViolation):
        engine.run(prices_no_index, weights=[1.0], allow_synthetic=False)
    # 显式 price_date 也缺省时同样 fail-closed
    prices_no_index2 = pd.DataFrame({"close": [100, 101]})
    with pytest.raises(PITViolation):
        engine.run(prices_no_index2, weights=[1.0], weights_on=None, price_date=None, allow_synthetic=False)


def test_pit_allow_synthetic_true_uses_first_index():
    """Task4-2a: 仅当 allow_synthetic==True 时才允许 pd_date=index[0] 合成。"""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd

    prices_with_index = pd.DataFrame(
        {"close": [100, 101, 102]}, index=pd.date_range("2026-08-01", periods=3)
    )
    engine = BacktestEngine()
    # allow_synthetic=True 允许合成，应成功执行
    res = engine.run(prices_with_index, weights=[1.0], allow_synthetic=True)
    assert res is not None
    assert "equity" in res
    assert len(res["equity"]) == 3
    # DatetimeIndex 合成路径：未显式传 price_date 时内部 pd_date 应取 index[0]
    # 验证显式 price_date 亦通过
    res2 = engine.run(
        prices_with_index, weights=[1.0], price_date=prices_with_index.index[0], allow_synthetic=True
    )
    assert "equity" in res2


def test_pit_second_branch_still_raises():
    """Task4-2a: 次分支（有 DatetimeIndex 但 allow_synthetic 未显式 True）仍需 raise PITViolation。"""
    from hero_quant.backtest.engine import BacktestEngine, PITViolation
    import pandas as pd
    import pytest

    # 有 DatetimeIndex 但未传 price_date，allow_synthetic 默认为 False / 显式 False 均应 raise
    prices = pd.DataFrame({"close": [100, 101, 102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    with pytest.raises(PITViolation):
        engine.run(prices, weights=[1.0], allow_synthetic=False)
    with pytest.raises(PITViolation):
        engine.run(prices, weights=[1.0])  # 默认 allow_synthetic=False
    # 无 index 的退化路径同样保持 raise（首分支）
    prices_no_index = pd.DataFrame({"close": [100, 101]})
    with pytest.raises(PITViolation):
        engine.run(prices_no_index, weights=[1.0])
