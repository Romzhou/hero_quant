# tests/test_quantlib_vector.py — TDD Task 6 vector parity
import pandas as pd
import math

def test_sma_vector_equal_pandas():
    s = pd.Series([1, 2, 3, 4, 5])
    from hero_quant.quantlib.indicators import sma
    from hero_quant.quantlib.polars_base import sma_polars

    a = sma(s, 3)
    b = sma_polars(s, 3)
    # NaN-aware parity: pandas NaN != NaN, so compare with handling
    assert len(a) == len(b)
    assert a.index.tolist() == b.index.tolist()
    # use pandas testing for NaN-aware (preferred)
    pd.testing.assert_series_equal(a, b, check_dtype=False, check_names=False)
    # also verify tolist with NaN handling mirrors spec intent
    assert all((math.isnan(x) and math.isnan(y)) or x == y for x, y in zip(a.tolist(), b.tolist()))
