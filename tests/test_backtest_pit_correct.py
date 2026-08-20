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
