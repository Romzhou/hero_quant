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
