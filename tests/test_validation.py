# tests/test_validation.py
def test_validation_rejects_future_data():
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd

    prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range("2026-08-10", periods=2))
    # 用未来收盘做当日权重应被拦
    try:
        validate(prices, weights_on="2026-08-09", price_date="2026-08-10")
    except ValidationError:
        pass
    else:
        assert False
