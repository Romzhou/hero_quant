# tests/test_quantlib.py
def test_sma_rsi():
    from hero_quant.quantlib.indicators import sma, rsi
    import pandas as pd
    s = pd.Series([1,2,3,4,5])
    assert sma(s, 3).iloc[-1] == 4.0
    assert 0 <= rsi(s, 14).iloc[-1] <= 100
