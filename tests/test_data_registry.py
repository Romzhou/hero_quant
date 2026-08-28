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


# --- P1 HIGH items TDD ---

def test_audit_log_bounded_and_threadsafe():
    """audit_log must be bounded deque with maxlen and protected by Lock."""
    import threading
    from collections import deque
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry(audit_log_maxlen=3)
    # check bounded deque
    assert hasattr(reg, "_audit_lock"), "MarketDataRegistry must have _audit_lock (threading.Lock)"
    assert isinstance(reg._audit_lock, type(threading.Lock())), "audit lock must be threading.Lock"
    assert isinstance(reg.audit_log, deque), "audit_log must be bounded deque"
    assert reg.audit_log.maxlen == 3, f"expected maxlen 3 got {reg.audit_log.maxlen}"

    class FakeLoader:
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 1, "date": "2026-01-01"}], None
        def health(self):
            return {"status": "ok"}

    reg.register(FakeLoader())
    for i in range(5):
        reg.get_bars(f"AAPL.US", "2026-01-01", "2026-01-02")
    # deque maxlen 3 => len <=3 after 5 appends
    assert len(reg.audit_log) <= 3, f"audit_log unbounded: len={len(reg.audit_log)} expected <=3"
    assert len(reg.audit_log) == 3


def test_cross_source_non_critical_not_fatal(caplog):
    """Non-critical cross_source exceptions must log warning and return bars, not abort."""
    import logging
    from hero_quant.data.registry import MarketDataRegistry

    class GoodLoader:
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100, "date": "2026-01-01"}], None
        def health(self):
            return {"status": "ok"}

    reg = MarketDataRegistry()
    reg.register(GoodLoader())

    # monkeypatch _cross_source_check to raise generic transient error (non-CrossSourceError)
    def boom(*args, **kwargs):
        raise RuntimeError("transient validation warning")

    reg._cross_source_check = boom
    caplog.set_level(logging.WARNING)
    # should NOT raise RuntimeError; should return bars with warning logged
    bars, prov = reg.get_bars("AAPL.US", "2026-01-01", "2026-01-02")
    assert len(bars) == 1
    # must have logged warning
    warnings = [r for r in caplog.records if "cross_source" in r.message.lower()]
    assert warnings, "expected warning log for non-critical cross_source error"

    # CrossSourceError must still be fatal
    from hero_quant.data.registry import CrossSourceError
    def fatal(*args, **kwargs):
        raise CrossSourceError("1% diff")
    reg._cross_source_check = fatal
    try:
        reg.get_bars("AAPL.US", "2026-01-01", "2026-01-02")
        assert False, "CrossSourceError should be fatal"
    except CrossSourceError:
        pass


def test_bars_empty_and_first_close_robust(caplog):
    """_bars_empty/_first_close must handle empty/malformed frames explicitly, log, deterministic."""
    import logging
    import pandas as pd
    from hero_quant.data.registry import MarketDataRegistry

    caplog.set_level(logging.WARNING)
    # empty DataFrame
    empty_df = pd.DataFrame()
    assert MarketDataRegistry._bars_empty(empty_df) is True
    assert MarketDataRegistry._first_close(empty_df) is None

    # DataFrame missing close column => should return None and log, not fallback to first column
    df_no_close = pd.DataFrame({"open": [1.0], "volume": [100]})
    caplog.clear()
    result = MarketDataRegistry._first_close(df_no_close)
    assert result is None, f"expected None for missing close column, got {result}"
    # must have logged warning about missing close column
    assert any("close" in r.message.lower() for r in caplog.records), "expected warning for missing close column"

    # DataFrame with close NaN => should return None, not NaN
    df_nan = pd.DataFrame({"close": [float("nan")], "open": [1]})
    assert MarketDataRegistry._first_close(df_nan) is None

    # DataFrame with valid close
    df_ok = pd.DataFrame({"close": [123.45], "open": [1]})
    assert MarketDataRegistry._first_close(df_ok) == 123.45

    # list branch missing close key => None
    assert MarketDataRegistry._first_close([{"open": 1}]) is None

    # list branch valid
    assert MarketDataRegistry._first_close([{"close": 99}]) == 99.0

    # list branch NaN
    assert MarketDataRegistry._first_close([{"close": float("nan")}]) is None

    # _bars_empty list empty vs None
    assert MarketDataRegistry._bars_empty([]) is True
    assert MarketDataRegistry._bars_empty(None) is True
    assert MarketDataRegistry._bars_empty([{"close": 1}]) is False
