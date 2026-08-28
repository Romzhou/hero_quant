def test_registry_fallback_and_provenance(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.registry import MarketDataRegistry
    from hero_quant.data.loaders.tencent import TencentLoader

    reg = MarketDataRegistry()
    reg.register(TencentLoader())
    bars, prov = reg.get_bars("600519.SH", "1d", "2026-08-01", "2026-08-19")
    assert len(bars) > 0
    assert prov.source == "synthetic"
    assert prov.unit in ("board_lots", "shares")


def test_missing_extra_raises_actionable():
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry()
    try:
        reg.get_bars("AAPL.US", "1d", "2026-08-01", "2026-08-19")
    except ImportError as e:
        assert "pip install" in str(e)
    else:
        assert False
