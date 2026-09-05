"""Lane D1 retest fixes — TDD-light repro tests. Appended per file; never edit existing test files."""

import logging

import pytest


# ---------------- registry.py (8 items: 3 high + 4 medium + 1 low) ----------------

def test_d1_registry_settings_failure_not_cached(monkeypatch):
    """High: transient Settings() failure must not be cached as synthetic forever."""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    from hero_quant.data import registry as regmod

    orig = s.Settings

    def _boom(*a, **k):
        raise OSError("transient settings outage")

    monkeypatch.setattr(s, "Settings", _boom)
    regmod.clear_settings_cache()
    assert regmod._get_data_mode() == "synthetic"  # fail-closed for this call
    monkeypatch.setattr(s, "Settings", orig)  # outage over
    assert regmod._get_data_mode() == "live"  # must retry, not sticky synthetic


def test_d1_registry_nonstring_data_mode_fail_closed(monkeypatch):
    """Medium: non-string data_mode (None/Enum/bytes) must default to synthetic, not live."""
    from hero_quant.data import registry as regmod
    import hero_quant.config.settings as s

    for bad in (None, b"live", 123):
        class _FakeSettings:
            data_mode = bad

        monkeypatch.setattr(s, "Settings", _FakeSettings)
        assert regmod._get_data_mode(force_refresh=True) == "synthetic"


def _make_cross_source_reg():
    from hero_quant.data.registry import MarketDataRegistry, Provenance

    class LiveLoader:
        markets = ["US"]
        unit = "shares"
        source = "yahoo"
        name = "yahoo"

        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100.0, "date": "2026-08-01"}], Provenance(
                source="yahoo", unit="shares", symbol=symbol
            )

        def health(self):
            return {"ok": True}

    class SynthLoader:
        markets = ["US"]
        unit = "shares"
        source = "synthetic"
        name = "synthetic"

        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100.0, "date": "2026-08-01"}], Provenance(
                source="synthetic", unit="shares", symbol=symbol
            )

        def health(self):
            return {"ok": True}

    reg = MarketDataRegistry()
    reg.register(LiveLoader())
    reg.register(SynthLoader())
    return reg


def test_d1_registry_synthetic_opt_in_via_extra():
    """High: extra={'allow_synthetic_comparison': True} must opt in; absent must raise."""
    from hero_quant.data.registry import CrossSourceError, Provenance

    reg = _make_cross_source_reg()
    bars = [{"close": 100.0, "date": "2026-08-01"}]
    plain = Provenance(source="yahoo", unit="shares", symbol="AAPL.US")
    with pytest.raises(CrossSourceError):
        reg._cross_source_check("AAPL.US", bars, plain, "1d", "2026-08-01", "2026-08-05")
    optin = Provenance(
        source="yahoo", unit="shares", symbol="AAPL.US",
        extra={"allow_synthetic_comparison": True},
    )
    reg._cross_source_check("AAPL.US", bars, optin, "1d", "2026-08-01", "2026-08-05")  # no raise


def test_d1_registry_cross_source_skip_logged(caplog):
    """Medium: integrity-gate skips (<2 loaders / missing bounds) must log a warning."""
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry()
    with caplog.at_level(logging.WARNING, logger="hero_quant.data.registry"):
        reg._cross_source_check("AAPL.US", [{"close": 1}], None, "1d", "2026-08-01", "2026-08-02")
        reg._cross_source_check("AAPL.US", None, None)
    assert any("skipped" in r.message for r in caplog.records)


def test_d1_registry_audit_log_snapshot_accessor():
    """Medium: locked get_audit_log() snapshot accessor exists and reflects appends."""
    from hero_quant.data.registry import MarketDataRegistry, Provenance

    class LiveLoader:
        markets = ["US"]
        unit = "shares"
        source = "yahoo"
        name = "yahoo"

        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100.0, "date": "2026-08-01"}], Provenance(
                source="yahoo", unit="shares", symbol=symbol
            )

        def health(self):
            return {"ok": True}

    reg = MarketDataRegistry()
    assert reg.get_audit_log() == []
    reg.register(LiveLoader())
    reg.get_bars("AAPL.US", "2026-08-01", "2026-08-05")
    log = reg.get_audit_log()
    assert isinstance(log, list) and len(log) == 1 and log[0]["symbol"] == "AAPL.US"


def test_d1_registry_traits_threadsafe():
    """Medium: register_trait/list_sources consistent under concurrent registration."""
    import threading

    from hero_quant.data.registry import MarketDataRegistry

    class T:
        pass

    reg = MarketDataRegistry()
    reg.register_trait("t1", T)
    assert reg.list_sources() == ["t1"]
    errs = []

    def _w(i):
        try:
            reg.register_trait(f"tx{i}", T)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=_w, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert len(reg.list_sources()) == 9


def test_d1_registry_non_import_error_not_masked():
    """High: network/data errors (TimeoutError) must re-raise unwrapped, not as ImportError."""
    from hero_quant.data.registry import MarketDataRegistry

    class FlakyLoader:
        markets = ["CN"]
        unit = "board_lots"
        source = "tencent"
        name = "tencent"

        def get_bars(self, symbol, start, end, interval="1d"):
            raise TimeoutError("net down")

        def health(self):
            return {"ok": False}

    reg = MarketDataRegistry()
    reg.register(FlakyLoader())
    with pytest.raises(TimeoutError):
        reg.get_bars("600519.SH", "2026-08-01", "2026-08-05")


def test_d1_registry_resolve_provenance_minimal_signature(monkeypatch):
    """Low: _resolve_provenance usable with loader only; legacy 3-arg call still works."""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    from hero_quant.data.registry import Provenance, _get_data_mode, _resolve_provenance

    _get_data_mode(force_refresh=True)

    class LiveLoader:
        source = "yahoo"
        name = "yahoo"

    assert _resolve_provenance(LiveLoader()) == "yahoo"
    assert _resolve_provenance(LiveLoader(), [{"close": 1}], None) == "yahoo"
    assert _resolve_provenance(
        LiveLoader(), [{"close": 1}], Provenance(source="", unit="", symbol="AAPL.US")
    ) == "yahoo"
