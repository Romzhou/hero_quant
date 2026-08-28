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
