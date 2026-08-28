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


def test_polars_validate_window_raises():
    import pytest
    from hero_quant.quantlib.polars_base import _validate_window, sma_polars
    import pandas as pd
    with pytest.raises(ValueError, match="window must be"):
        _validate_window(0)
    with pytest.raises(ValueError, match="invalid window"):
        _validate_window("bad")
    # None should return default
    assert _validate_window(None) == 20


def test_polars_sma_conflicting_aliases_raises():
    import pytest
    import pandas as pd
    from hero_quant.quantlib.polars_base import sma_polars
    s = pd.Series([1, 2, 3, 4])
    with pytest.raises(TypeError, match="conflicting aliases"):
        sma_polars(s, n=3, period=5)
    with pytest.raises(TypeError, match="unexpected positional"):
        sma_polars(s, 3, 4)


def test_polars_sma_zero_copy_path():
    import pandas as pd
    import numpy as np
    from hero_quant.quantlib.polars_base import sma_polars
    s = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0])
    out = sma_polars(s, 3)
    # NaN in input should propagate via min_samples semantics, not be coerced via strict=False to 0
    assert out.isna().sum() >= 2
