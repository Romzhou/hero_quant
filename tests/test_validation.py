# tests/test_validation.py
def test_validation_rejects_future_data():
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd

    prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range("2026-08-10", periods=2))
    # PIT correct: weights_on <= price_date is valid, should NOT raise
    validate(prices, weights_on="2026-08-09", price_date="2026-08-10")
    # equal date also valid
    validate(prices, weights_on="2026-08-10", price_date="2026-08-10")
    # weights_on > price_date must raise (uses future data)
    try:
        validate(prices, weights_on="2026-08-11", price_date="2026-08-10")
    except ValidationError:
        pass
    else:
        assert False, "expected ValidationError for weights_on > price_date"
