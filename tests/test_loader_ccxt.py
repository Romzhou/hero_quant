def test_ccxt_loader_binance_spot(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader

    loader = CCXTLoader()
    # trait contract checks
    assert loader.unit == "shares"
    assert loader.markets == ["CRYPTO"]
    assert loader.name == "ccxt"
    assert loader.source == "ccxt"
    df = loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-05")
    # columns first 3 must be open/high/low per spec
    assert list(df.columns)[:3] == ["open", "high", "low"]
    assert "volume" in df.columns
    assert len(df) >= 5
    # health should report ccxt availability
    h = loader.health()
    assert "ccxt_available" in h or "status" in h


def test_ccxt_loader_interval_mapping(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader

    loader = CCXTLoader()
    # interval param should be accepted and mapped
    df = loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-05", interval="1h")
    assert list(df.columns)[:3] == ["open", "high", "low"]
    assert len(df) >= 5


def test_ccxt_loader_health_shape():
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader

    loader = CCXTLoader()
    h = loader.health()
    assert h["source"] == "ccxt"
    assert h["unit"] == "shares"
    assert "markets" in h


# --- P1 TDD: settings narrow except / unknown mode / limit math ---
import pytest
import importlib as _importlib2
import logging
import unittest.mock as mock
from datetime import datetime


def _reload_ccxt_settings(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import hero_quant.config.settings as s
    _importlib2.reload(s)
    return s


def test_ccxt_settings_narrow_except(monkeypatch):
    _reload_ccxt_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader
    loader = CCXTLoader()
    with mock.patch("hero_quant.config.settings.Settings", side_effect=RuntimeError("config broken")):
        try:
            df = loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-05")
            pytest.fail("should have propagated RuntimeError, not returned df")
        except RuntimeError as e:
            assert "config broken" in str(e)


def test_ccxt_unknown_mode_raises(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "unknown_mode_xyz")
    import hero_quant.config.settings as s
    _importlib2.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader
    loader = CCXTLoader()
    with pytest.raises((ValueError, RuntimeError)):
        loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-05")


def test_ccxt_intraday_limit_correct_and_truncation_log(monkeypatch, caplog):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    _importlib2.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader
    loader = CCXTLoader()
    caplog.set_level(logging.WARNING)
    captured = {}
    fake_exchange = mock.MagicMock()

    def fake_fetch(symbol, timeframe, since, limit):
        captured["limit"] = limit
        captured["timeframe"] = timeframe
        ohlcv = []
        base_ts = int(datetime(2025, 1, 1).timestamp() * 1000)
        for i in range(10):
            ohlcv.append([base_ts + i * 3600 * 1000, 100 + i, 101 + i, 99 + i, 100 + i, 10])
        return ohlcv

    fake_exchange.fetch_ohlcv.side_effect = fake_fetch
    fake_ccxt = mock.MagicMock()
    fake_ccxt.binance.return_value = fake_exchange
    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt}):
        df = loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-10", interval="15m")
        assert captured["limit"] == 960, f"limit miscalculated {captured['limit']} != 960"
        assert any("truncat" in r.message.lower() for r in caplog.records), "expected truncation warning"


def test_ccxt_intraday_limit_1h(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    _importlib2.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader
    loader = CCXTLoader()
    captured = {}
    fake_exchange = mock.MagicMock()

    def fake_fetch(symbol, timeframe, since, limit):
        captured["limit"] = limit
        return [[int(datetime(2025, 1, 1).timestamp() * 1000), 100, 101, 99, 100, 10]]

    fake_exchange.fetch_ohlcv.side_effect = fake_fetch
    fake_ccxt = mock.MagicMock()
    fake_ccxt.binance.return_value = fake_exchange
    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt}):
        loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-03", interval="1h")
        assert captured["limit"] == 72
