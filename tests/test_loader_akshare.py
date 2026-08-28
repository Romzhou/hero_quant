def test_akshare_loader_fallback_synthetic(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.akshare_loader import AKShareLoader

    loader = AKShareLoader()
    df = loader.get_bars("600519.SH", "2025-01-01", "2025-01-10")
    assert list(df.columns)[:3] == ["open", "high", "low"]
    assert len(df) >= 5


# --- P1 TDD: silent fallback / legacy swap / volume heuristic ---
import pytest
import importlib as _importlib
import logging


def _reload_settings(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import hero_quant.config.settings as s
    _importlib.reload(s)
    return s


def test_akshare_synthetic_bad_dates_raises_in_live(monkeypatch):
    _reload_settings(monkeypatch, "live")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    loader = AKShareLoader()
    with pytest.raises((ValueError, Exception)):
        loader._synthetic_df("600519.SH", "bad-start", "bad-end")


def test_akshare_synthetic_bad_dates_warn_in_synthetic(monkeypatch, caplog):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    loader = AKShareLoader()
    caplog.set_level(logging.WARNING)
    df = loader._synthetic_df("600519.SH", "bad-start", "bad-end")
    assert len(df) > 0


def test_akshare_live_bad_dates_raises(monkeypatch):
    _reload_settings(monkeypatch, "live")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    loader = AKShareLoader()
    import unittest.mock as mock
    fake_ak = mock.MagicMock()
    fake_ak.stock_zh_a_hist.return_value = None
    with mock.patch.dict("sys.modules", {"akshare": fake_ak}):
        with pytest.raises((ValueError, RuntimeError)):
            loader.get_bars("600519.SH", "not-a-date", "also-bad")


def test_akshare_legacy_swap_unambiguous(monkeypatch):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    loader = AKShareLoader()
    df = loader.get_bars("600519.SH", "1d", "2025-01-01", interval="2025-01-10")
    assert len(df) > 0


def test_akshare_legacy_swap_ambiguous_raises(monkeypatch):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    loader = AKShareLoader()
    with pytest.raises(ValueError):
        loader.get_bars("600519.SH", "1d", "notadate", interval="1d")


def test_akshare_volume_heuristic_deterministic(monkeypatch, caplog):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    import pandas as pd
    loader = AKShareLoader()
    caplog.set_level(logging.WARNING)
    df_input = pd.DataFrame({
        "date": ["2025-01-01", "2025-01-02"],
        "open": [10, 11],
        "close": [10, 11],
        "high": [12, 13],
        "low": [9, 10],
        "volume": [0, 200000],
    })
    # Use Chinese columns path also deterministic
    df_ak = pd.DataFrame({
        "\u65e5\u671f": ["2025-01-01", "2025-01-02"],
        "\u5f00\u76d8": [10, 11],
        "\u6536\u76d8": [10, 11],
        "\u6700\u9ad8": [12, 13],
        "\u6700\u4f4e": [9, 10],
        "\u6210\u4ea4\u91cf": [0, 200000],
    })
    result = loader._normalize_akshare(df_ak)
    assert result is not None
    result2 = loader._normalize_akshare(df_ak)
    assert result["volume"].tolist() == result2["volume"].tolist()
    # heuristic should have logged deterministically if needed (no swallowed silently)
    # 0 stays 0 after heuristic division, 200000 -> 2000
    assert result["volume"].iloc[0] == 0.0
    assert result["volume"].iloc[1] == 2000.0
