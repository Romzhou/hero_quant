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


def test_provenance_consistent_across_blocks(monkeypatch):
    """Same loader class+name yields identical provenance from all 3 code paths."""
    from hero_quant.data.registry import MarketDataRegistry, Provenance

    class SyntheticLikeLoader:
        markets = ["US"]
        unit = "shares"
        source = "synthetic_alt"
        name = "my_synthetic_loader"

        def get_bars(self, symbol, start, end, interval="1d"):
            # path A: tuple with prov None -> triggers block 1
            # we will test all 3 blocks via direct registry calls with different return shapes
            # here just return tuple prov None; other blocks tested via helper
            return [{"close": 100, "date": "2026-08-01"}], None

    class RealLoader:
        markets = ["US"]
        unit = "shares"

        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100, "date": "2026-08-01"}], None

    # Need to test helper directly if exists, otherwise test via get_bars paths
    try:
        from hero_quant.data.registry import _resolve_provenance, _get_data_mode

        # same loader yields same result regardless of prov/result combos
        s1 = _resolve_provenance(SyntheticLikeLoader(), [{"close": 1}], None)
        s2 = _resolve_provenance(SyntheticLikeLoader(), [{"close": 1}], Provenance(source="", unit="", symbol="AAPL.US"))
        s3 = _resolve_provenance(SyntheticLikeLoader(), [{"close": 1}], Provenance(source="synthetic", unit="shares", symbol="AAPL.US"))
        assert s1 == s2 == "synthetic"
        # real loader without synthetic markers should infer via class name (but class name not synthetic)
        # it should not be synthetic when data_mode is live
        monkeypatch.setenv("HERO_DATA_MODE", "live")
        import importlib, hero_quant.config.settings as s

        importlib.reload(s)
        # clear cache if exists
        try:
            import hero_quant.data.registry as regmod

            if hasattr(regmod, "_cached_mode"):
                regmod._cached_mode = None
            if hasattr(regmod, "_settings_mode_cache"):
                regmod._settings_mode_cache = None
        except Exception:
            pass
        r1 = _resolve_provenance(RealLoader(), [{"close": 1}], None)
        r2 = _resolve_provenance(RealLoader(), [{"close": 1}], Provenance(source="", unit="", symbol="AAPL"))
        assert r1 == r2
    except ImportError:
        # if helper not yet extracted, test via registry get_bars consistency
        reg = MarketDataRegistry()
        loader = SyntheticLikeLoader()
        reg.register(loader)
        # monkeypatch Settings to simulate different blocks? just assert helper exists
        assert False, "_resolve_provenance helper not found - fix not applied"


def test_settings_failure_defaults_synthetic_consistent(monkeypatch):
    """Settings failure path must be consistent fail-closed to synthetic."""
    import hero_quant.data.registry as regmod
    from hero_quant.data.registry import _resolve_provenance

    class SynLoader:
        markets = ["US"]
        unit = "shares"

        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 1}], None

    # monkeypatch Settings to raise
    monkeypatch.setattr("hero_quant.config.settings.Settings", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    # clear cache
    for attr in ("_cached_mode", "_settings_mode_cache", "_cached_data_mode"):
        if hasattr(regmod, attr):
            setattr(regmod, attr, None)

    s1 = _resolve_provenance(SynLoader(), [{"close": 1}], None)
    s2 = _resolve_provenance(SynLoader(), [{"close": 1}], None)
    assert s1 == s2 == "synthetic", f"expected synthetic on Settings failure, got {s1},{s2}"

    # also verify helper only instantiates Settings once (cached) - check via call count if possible
    # we test consistency, not count, to avoid brittle
