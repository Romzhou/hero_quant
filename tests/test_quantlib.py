# tests/test_quantlib.py
def test_sma_rsi():
    from hero_quant.quantlib.indicators import sma, rsi
    import pandas as pd
    s = pd.Series([1,2,3,4,5])
    assert sma(s, 3).iloc[-1] == 4.0
    assert 0 <= rsi(s, 14).iloc[-1] <= 100

import pytest

def test_validate_window_rejects_invalid():
    """P2-2: _validate_window must raise clear error for invalid window, not silent fallback."""
    from hero_quant.quantlib.indicators import _validate_window, sma
    with pytest.raises((ValueError, TypeError)):
        _validate_window("bad")
    with pytest.raises((ValueError, TypeError)):
        _validate_window(0)
    with pytest.raises((ValueError, TypeError)):
        _validate_window(-5)
    with pytest.raises((ValueError, TypeError)):
        _validate_window(None)
    # sma should propagate error
    import pandas as pd
    s = pd.Series([1,2,3])
    with pytest.raises((ValueError, TypeError)):
        sma(s, window="bad")

def test_to_series_narrow_exception():
    """P2-2: _to_series must narrow exception and raise for unconvertible input (log exc_info)."""
    from hero_quant.quantlib.indicators import _to_series
    # valid list still works
    import pandas as pd
    s = _to_series([1,2,3])
    assert len(s) == 3
    # object that fails Series construction should raise TypeError, not silent empty
    class Bad:
        def __iter__(self):
            raise ValueError("bad iter")
    with pytest.raises((TypeError, ValueError)):
        _to_series(Bad())
