import pandas as pd
import pytest

from hero_quant.data.registry import CrossSourceError, MarketDataRegistry


def test_cross_source_block():
    r = MarketDataRegistry()
    df_a = pd.DataFrame({"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]})
    df_b = pd.DataFrame({"open": [103], "high": [104], "low": [102], "close": [103], "volume": [1000]})
    with pytest.raises(CrossSourceError):
        r._cross_source_check("600519.SH", df_a, df_b)


def test_cross_source_within_threshold_no_raise():
    r = MarketDataRegistry()
    df_a = pd.DataFrame({"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]})
    df_b = pd.DataFrame({"open": [100.5], "high": [101], "low": [99], "close": [100.5], "volume": [1000]})
    # diff 0.5% should not raise
    r._cross_source_check("600519.SH", df_a, df_b)
