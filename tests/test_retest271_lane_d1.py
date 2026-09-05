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


# ---------------- loaders/tencent.py (6 items: 2 high + 3 medium + 1 low) ----------------

def _tencent_loader_live(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader
    return TencentLoader()


def _mock_urlopen_json(monkeypatch, payload):
    import json
    import unittest.mock as mock
    import urllib.request

    text = json.dumps(payload).encode()
    m = mock.MagicMock()
    m.read.return_value = text
    m.__enter__ = lambda self: self
    m.__exit__ = lambda self, *a: False
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=2: m)
    monkeypatch.setattr("hero_quant.data.loaders.tencent.time.sleep", lambda *a, **k: None)


def test_d1_tencent_cache_mode_isolated(monkeypatch):
    """High: synthetic result must not poison a later live read (mode in cache key / bypass)."""
    from hero_quant.data.loaders.tencent import TencentLoader

    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    loader = TencentLoader()
    synth = loader.get_bars("600519.SH", "2025-01-01", "2025-01-03")
    assert len(synth) == 3
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    importlib.reload(s)
    _mock_urlopen_json(monkeypatch, {"data": {"sh600519": {"day": [["2025-01-01", 10, 11, 12, 9, 100]]}}})
    live = loader.get_bars("600519.SH", "2025-01-01", "2025-01-03")
    assert len(live) == 1 and live[0]["close"] == 11.0  # not the 3 synthetic bars


def test_d1_tencent_explicit_qfqday_key(monkeypatch):
    """High: metadata lists (qt) must not be picked over qfq-day bars; absent key fails closed."""
    from hero_quant.data.loaders.tencent import TencentLoader

    loader = _tencent_loader_live(monkeypatch)
    _mock_urlopen_json(monkeypatch, {"data": {
        "qt": [["2025-01-01", 999, 999, 999, 999, 999]],
        "sh600519": {"day": [["2025-01-02", 10, 11, 12, 9, 100]]},
    }})
    bars = loader.get_bars("600519.SH", "2025-01-01", "2025-01-05")
    assert bars[0]["date"] == "2025-01-02" and bars[0]["close"] == 11.0
    _mock_urlopen_json(monkeypatch, {"data": {"sz000001": {"qt": [["2025-01-02", 10, 11, 12, 9, 100]]}}})
    with pytest.raises((ValueError, RuntimeError)):
        loader.get_bars("600519.SH", "2025-01-06", "2025-01-09")


def test_d1_tencent_live_dates_validated(monkeypatch):
    """Medium: malformed live bar dates (None) must fail closed via DataValidationError."""
    from hero_quant.data.loaders.tencent import DataValidationError, TencentLoader

    loader = _tencent_loader_live(monkeypatch)
    _mock_urlopen_json(monkeypatch, {"data": {"sh600000": {"day": [[None, 10, 11, 12, 9, 100]]}}})
    with pytest.raises(DataValidationError):
        loader.get_bars("600000.SH", "2025-01-01", "2025-01-05")


def test_d1_tencent_dates_validated_before_url(monkeypatch):
    """Medium: injection-y start/end must fail closed before URL build; no urlopen call."""
    import urllib.request

    from hero_quant.data.loaders.tencent import DataValidationError

    loader = _tencent_loader_live(monkeypatch)
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
    with pytest.raises(DataValidationError):
        loader.get_bars("600001.SH", "2025-01-01&evil=1", "2025-01-05")
    assert called == []


def test_d1_tencent_malformed_payload_fail_closed(monkeypatch):
    """Medium: None numeric field must fail closed (DataValidationError/RuntimeError), never raw TypeError."""
    import json
    import unittest.mock as mock
    import urllib.request

    from hero_quant.data.loaders.tencent import DataValidationError

    loader = _tencent_loader_live(monkeypatch)
    text = json.dumps({"data": {"sh600002": {"day": [["2025-01-02", None, 11, 12, 9, 100]]}}}).encode()
    m = mock.MagicMock()
    m.read.return_value = text
    m.__enter__ = lambda self: self
    m.__exit__ = lambda self, *a: False
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=2: m)
    monkeypatch.setattr("hero_quant.data.loaders.tencent.time.sleep", lambda *a, **k: None)
    with pytest.raises((DataValidationError, RuntimeError)):
        loader.get_bars("600002.SH", "2025-01-01", "2025-01-05")


def test_d1_tencent_json_decode_taxonomy(monkeypatch):
    """Low: garbage body must surface as RuntimeError (network/parse), handler sane."""
    import unittest.mock as mock
    import urllib.request

    loader = _tencent_loader_live(monkeypatch)
    m = mock.MagicMock()
    m.read.return_value = b"not json {{{"
    m.__enter__ = lambda self: self
    m.__exit__ = lambda self, *a: False
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=2: m)
    monkeypatch.setattr("hero_quant.data.loaders.tencent.time.sleep", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        loader.get_bars("600003.SH", "2025-01-01", "2025-01-05")


# ---------------- loaders/akshare_loader.py (5 items: 1 high + 4 medium) ----------------

def _akshare_loader(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    return AKShareLoader()


def test_d1_akshare_weekly_rejected(monkeypatch):
    """High: 1wk/1mo/1W must fail closed, not silently return daily bars."""
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    loader = _akshare_loader(monkeypatch, "synthetic")
    for iv in ("1wk", "1mo", "1W", "1m", "1h"):
        with pytest.raises(DataValidationError):
            loader.get_bars("600519.SH", "2025-01-01", "2025-01-05", iv)


def test_d1_akshare_unknown_mode_fail_closed(monkeypatch):
    """Medium: unknown/non-string data_mode must raise, never fail open to live."""
    import hero_quant.config.settings as s
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    monkeypatch.setenv("HERO_DATA_MODE", "synthetc")
    import importlib
    importlib.reload(s)
    with pytest.raises(DataValidationError):
        AKShareLoader().get_bars("600519.SH", "2025-01-01", "2025-01-05")

    class _BadSettings:
        data_mode = 123

    monkeypatch.setattr(s, "Settings", _BadSettings)
    with pytest.raises(DataValidationError):
        AKShareLoader().get_bars("600519.SH", "2025-01-01", "2025-01-05")


def test_d1_akshare_live_dates_guarded(monkeypatch):
    """Medium: None dates / inverted range on live path must fail closed pre-network."""
    import sys
    import types

    from hero_quant.data.loaders.akshare_loader import DataValidationError

    fake_ak = types.ModuleType("akshare")
    fake_ak.stock_zh_a_hist = lambda **k: (_ for _ in ()).throw(AssertionError("network must not be hit"))
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    loader = _akshare_loader(monkeypatch, "live")
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", None, "2025-01-05")
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", "2025-01-05", "2025-01-01")


def test_d1_akshare_missing_ohlc_named(monkeypatch):
    """Medium: missing OHLC column must raise naming the column, not generic no-bars."""
    import pandas as pd
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    loader = _akshare_loader(monkeypatch, "synthetic")
    df = pd.DataFrame({"日期": ["2025-01-01"], "开盘": [10.0], "收盘": [11.0], "最高": [12.0], "成交量": [100]})
    with pytest.raises(DataValidationError, match="low"):
        loader._normalize_akshare(df)


def test_d1_akshare_volume_lots_documented(monkeypatch):
    """Medium: volume normalization documents shares-vs-lots assumption (no silent 100x)."""
    import inspect

    from hero_quant.data.loaders.akshare_loader import AKShareLoader

    src = inspect.getsource(AKShareLoader._normalize_akshare)
    assert "board_lots" in src and "100" in src


# ---------------- loaders/ccxt_loader.py (4 items: 1 high + 2 medium + 1 low) ----------------

def _ccxt_loader(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader
    return CCXTLoader()


def _mock_ccxt(monkeypatch, ohlcv_or_exc):
    import sys
    import types

    class _Exch:
        def fetch_ohlcv(self, *a, **k):
            if isinstance(ohlcv_or_exc, BaseException):
                raise ohlcv_or_exc
            return ohlcv_or_exc

    fake = types.ModuleType("ccxt")

    class _BaseError(Exception):
        pass

    fake.BaseError = _BaseError
    fake.ExchangeError = type("ExchangeError", (_BaseError,), {})
    fake.binance = lambda *a, **k: _Exch()
    monkeypatch.setitem(sys.modules, "ccxt", fake)
    return fake


def test_d1_ccxt_exchange_error_wrapped(monkeypatch):
    """High: ccxt.BaseError subclasses must be wrapped in RuntimeError, not escape."""
    loader = _ccxt_loader(monkeypatch, "live")
    fake = _mock_ccxt(monkeypatch, None)
    with pytest.raises(RuntimeError, match="ccxt fetch failed"):
        loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-03")
    err = fake.ExchangeError("exchange down")
    _mock_ccxt(monkeypatch, err)
    with pytest.raises(RuntimeError, match="ccxt fetch failed"):
        loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-03")


def test_d1_ccxt_no_dead_limit_var(monkeypatch):
    """Medium: no unused `limit = min(1500, ...)` dead variable; pagination uses chunk_limit."""
    import inspect

    from hero_quant.data.loaders import ccxt_loader as m

    import re
    src = inspect.getsource(m.CCXTLoader.get_bars)
    assert not re.search(r"(?<![_a-z])limit\s*=\s*min\(1500", src)
    assert "chunk_limit" in src


def test_d1_ccxt_clip_fallback_respects_timeframe(monkeypatch):
    """Medium: clip-failure fallback must slice [:requested], not [:days]."""
    import inspect

    from hero_quant.data.loaders import ccxt_loader as m

    src = inspect.getsource(m.CCXTLoader.get_bars)
    assert "df.iloc[:requested]" in src
    assert "df.iloc[:days]" not in src


def test_d1_ccxt_no_unreachable_empty_check(monkeypatch):
    """Low: unreachable `if len(df) == 0: raise ValueError("empty df")` removed."""
    import inspect

    from hero_quant.data.loaders import ccxt_loader as m

    src = inspect.getsource(m.CCXTLoader.get_bars)
    assert 'raise ValueError("empty df")' not in src


# ---------------- loaders/news.py (4 items: 1 high + 3 medium) ----------------

def test_d1_news_mixed_tz_pit_verified():
    """High: naive snapshot vs aware publish_time must normalize to UTC, pit=True."""
    from hero_quant.data.loaders.news import load_news

    # aware publish 2024-01-04 08:00 UTC vs naive snapshot midnight 2024-01-05 -> UTC normalize -> verified
    recs = [{"id": 1, "trade_date": "2024-01-05", "publish_time": "2024-01-04T08:00:00+00:00"}]
    out = load_news(recs, trade_date="2024-01-05", snapshot_date="2024-01-05")
    assert out[0]["pit"] is True and out[0]["pit_status"] == "verified"
    # symmetric: naive publish vs aware snapshot also normalizes
    recs2 = [{"id": 2, "trade_date": "2024-01-05", "publish_time": "2024-01-04 08:00:00"}]
    out2 = load_news(recs2, trade_date="2024-01-05", snapshot_date="2024-01-05T00:00:00+00:00")
    assert out2[0]["pit"] is True and out2[0]["pit_status"] == "verified"


def test_d1_news_unparseable_date_is_missing():
    """Medium: unparseable trade_date must be missing (dropped), never prefix-matched."""
    from hero_quant.data.loaders.news import _normalize_date_str, load_news

    assert _normalize_date_str("not-a-date!!!") is None
    recs = [
        {"id": 1, "trade_date": "2024-01-05", "publish_time": "2024-01-04 10:00:00"},
        {"id": 2, "trade_date": "garbage-2024-01-05", "publish_time": "2024-01-04 10:00:00"},
    ]
    out = load_news(recs, trade_date="2024-01-05", snapshot_date="2024-01-05")
    assert [r["id"] for r in out] == [1]


def test_d1_news_schema_anomaly_warns_not_raises(caplog):
    """Medium: missing trade_date column entirely must warn + return [], not raise."""
    import logging

    from hero_quant.data.loaders.news import load_news

    recs = [
        {"id": 1, "publish_time": "2024-01-01 10:00:00"},
        {"id": 2, "publish_time": "2024-01-01 10:00:00"},
    ]
    with caplog.at_level(logging.WARNING, logger="hero_quant.data.loaders.news"):
        out = load_news(recs, trade_date="2024-01-02", snapshot_date="2024-01-02")
    assert out == []
    assert any("schema anomaly" in r.message for r in caplog.records)


def test_d1_news_generic_date_not_trade_date():
    """Medium: generic `date` must not be treated as trade_date label."""
    from hero_quant.data.loaders.news import _extract_trade_date

    assert _extract_trade_date({"date": "2024-01-05"}) is None
    assert _extract_trade_date({"trade_date": "2024-01-05"}) == "2024-01-05"


# ---------------- loaders/yahoo.py (4 items: 1 high + 3 medium) ----------------

def _yahoo_loader(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.yahoo import YahooLoader
    return YahooLoader()


def test_d1_yahoo_synthetic_empty_range_rejected(monkeypatch):
    """Medium: synthetic start>=end must raise DataValidationError, not return []."""
    from hero_quant.data.loaders.yahoo import DataValidationError

    loader = _yahoo_loader(monkeypatch, "synthetic")
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL.US", "2025-01-05", "2025-01-01")
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL.US", "2025-01-05", "2025-01-05")


def test_d1_yahoo_no_data_error_type(monkeypatch):
    """Medium: empty yfinance result must raise DataValidationError (a ValueError)."""
    import sys
    import types

    import pandas as pd
    from hero_quant.data.loaders.yahoo import DataValidationError

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **k: pd.DataFrame()
    fake_yf.Ticker = lambda *a, **k: types.SimpleNamespace(
        history=lambda *a, **k: pd.DataFrame()
    )
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    loader = _yahoo_loader(monkeypatch, "live")
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL.US", "2025-01-01", "2025-01-05")


def test_d1_yahoo_interval_normalized(monkeypatch):
    """Medium: 1D->1d / 1W->1wk normalized before forwarding to yfinance."""
    import sys
    import types

    import pandas as pd
    from hero_quant.data.loaders.yahoo import DataValidationError

    seen = {}
    idx = pd.to_datetime(["2025-01-02"])
    df = pd.DataFrame({"Open": [10.0], "High": [12.0], "Low": [9.0], "Close": [11.0], "Volume": [100.0]}, index=idx)
    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unused"))
    def _ticker(sym):
        class _T:
            def history(self, *a, **k):
                seen["interval"] = k.get("interval")
                return df
        return _T()
    fake_yf.Ticker = _ticker
    # download returns empty so history path runs
    fake_yf.download = lambda *a, **k: pd.DataFrame()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    loader = _yahoo_loader(monkeypatch, "live")
    loader.get_bars("AAPL.US", "2025-01-01", "2025-01-05", "1W")
    assert seen["interval"] == "1wk"
    loader.get_bars("AAPL.US", "2025-01-01", "2025-01-05", "1D")
    assert seen["interval"] == "1d"
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL.US", "2025-01-01", "2025-01-05", "2x")


def test_d1_yahoo_inf_rejected(monkeypatch):
    """High: inf OHLCV must be rejected via math.isfinite, not pass guards."""
    import sys
    import types

    import pandas as pd
    from hero_quant.data.loaders.yahoo import DataValidationError

    idx = pd.to_datetime(["2025-01-02"])
    df = pd.DataFrame({"Open": [float("inf")], "High": [12.0], "Low": [9.0], "Close": [11.0], "Volume": [100.0]}, index=idx)
    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **k: df
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    loader = _yahoo_loader(monkeypatch, "live")
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL.US", "2025-01-01", "2025-01-05")
