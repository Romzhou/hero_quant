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
