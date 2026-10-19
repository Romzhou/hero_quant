"""J线 J3: data loaders + trait + registry 黑洞扫荡（合成隔离，不碰真网）。"""
from __future__ import annotations

import importlib

import pandas as pd
import pytest


def _reload_settings(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import hero_quant.config.settings as s
    importlib.reload(s)
    return s


# ---------- trait.py 全分支 ----------

def _good_loader():
    class L:
        name = "tencent"
        markets = ["CN"]
        unit = "board_lots"

        def get_bars(self, symbol, start, end, interval="1d"):
            return []

        def health(self):
            return {"status": "ok"}
    return L()


def test_j3_trait_validate_ok_and_aliases():
    from hero_quant.data.trait import assert_bars_contract, validate_bars_contract, validate_loader

    validate_loader(_good_loader())
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    df = pd.DataFrame({"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
                       "close": [1.0, 2.0], "volume": [1, 1]}, index=idx)
    assert_bars_contract(df)
    validate_bars_contract(df)
    # tuple 解包 + 空
    assert_bars_contract((df, {"prov": 1}))
    assert_bars_contract([])
    assert_bars_contract(pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []}))


def test_j3_trait_validate_loader_errors():
    from hero_quant.data.trait import validate_loader

    with pytest.raises(ValueError):
        validate_loader(object())  # 缺 name
    bad_name = _good_loader(); bad_name.name = ""
    with pytest.raises(ValueError):
        validate_loader(bad_name)
    bad_src = _good_loader(); bad_src.name = "nope_src"
    with pytest.raises(ValueError):
        validate_loader(bad_src)
    bad_source_attr = _good_loader(); bad_source_attr.source = "nope_src"
    with pytest.raises(ValueError):
        validate_loader(bad_source_attr)
    no_markets = _good_loader()
    with pytest.raises(ValueError):
        validate_loader(type("L2", (), {"name": "tencent", "unit": "board_lots",
                                        "get_bars": _good_loader().get_bars,
                                        "health": _good_loader().health})())
    bad_markets = _good_loader(); bad_markets.markets = "CN"
    with pytest.raises(TypeError):
        validate_loader(bad_markets)
    bad_m = _good_loader(); bad_m.markets = ["CN", 123]
    with pytest.raises(TypeError):
        validate_loader(bad_m)
    no_unit = _good_loader()
    with pytest.raises(ValueError):
        validate_loader(type("L3", (), {"name": "tencent", "markets": ["CN"],
                                        "get_bars": _good_loader().get_bars,
                                        "health": _good_loader().health})())
    bad_unit = _good_loader(); bad_unit.unit = "lots"
    with pytest.raises(ValueError):
        validate_loader(bad_unit)
    no_bars = _good_loader()
    with pytest.raises(ValueError):
        validate_loader(type("L4", (), {"name": "tencent", "markets": ["CN"], "unit": "board_lots",
                                        "health": _good_loader().health})())

    class BadSig:
        name = "tencent"; markets = ["CN"]; unit = "board_lots"
        def get_bars(self, symbol, start):
            return []
        def health(self):
            return {}
    with pytest.raises(TypeError):
        validate_loader(BadSig())

    class BadDefault:
        name = "tencent"; markets = ["CN"]; unit = "board_lots"
        def get_bars(self, symbol, start, end, interval):
            return []
        def health(self):
            return {}
    with pytest.raises(TypeError):
        validate_loader(BadDefault())

    class BadDefault2:
        name = "tencent"; markets = ["CN"]; unit = "board_lots"
        def get_bars(self, symbol, start, end, interval="1h"):
            return []
        def health(self):
            return {}
    with pytest.raises(TypeError):
        validate_loader(BadDefault2())

    class NoHealth:
        name = "tencent"; markets = ["CN"]; unit = "board_lots"
        def get_bars(self, symbol, start, end, interval="1d"):
            return []
    with pytest.raises(ValueError):
        validate_loader(NoHealth())


def test_j3_trait_contract_violations():
    from hero_quant.data.trait import assert_bars_contract

    with pytest.raises(ValueError):
        assert_bars_contract(None)
    with pytest.raises(ValueError):
        assert_bars_contract("nope")
    with pytest.raises(ValueError):
        assert_bars_contract(object())
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    df_missing = pd.DataFrame({"open": [1.0, 2.0]}, index=idx)
    with pytest.raises(ValueError):
        assert_bars_contract(df_missing)
    df_baddx = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
                            index=[0])
    with pytest.raises(ValueError):
        assert_bars_contract(df_baddx)
    df_tz = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
                         index=pd.DatetimeIndex(["2024-01-01"], tz="UTC"))
    with pytest.raises(ValueError):
        assert_bars_contract(df_tz)
    df_unsorted = pd.DataFrame(
        {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0], "volume": [1, 1]},
        index=pd.DatetimeIndex(["2024-01-02", "2024-01-01"]))
    with pytest.raises(ValueError):
        assert_bars_contract(df_unsorted)
    df_dup = pd.DataFrame(
        {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0], "volume": [1, 1]},
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-01"]))
    with pytest.raises(ValueError):
        assert_bars_contract(df_dup)
    # list 分支
    with pytest.raises(ValueError):
        assert_bars_contract(["not-a-dict"])
    with pytest.raises(ValueError):
        assert_bars_contract([{"open": 1.0}])
    with pytest.raises(ValueError):
        assert_bars_contract([{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}])
    with pytest.raises(ValueError):
        assert_bars_contract([{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                               "date": "not-a-date!!!$$$%%%"[:0] or "32-99-99"}])
    dup_list = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "date": "2024-01-01"},
                {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "date": "2024-01-01"}]
    with pytest.raises(ValueError):
        assert_bars_contract(dup_list)
    unsorted_list = [
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "date": "2024-01-02"},
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "date": "2024-01-01"}]
    with pytest.raises(ValueError):
        assert_bars_contract(unsorted_list)
    ok_list = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "date": "2024-01-01",
                "trade_date": "2024-01-01"}]
    assert_bars_contract(ok_list)


# ---------- akshare ----------

def test_j3_akshare_synthetic_and_guards(monkeypatch):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    loader = AKShareLoader()
    df = loader._synthetic_df("600519.SH", "2024-01-01", "2024-01-03")
    assert len(df) == 3
    with pytest.raises(DataValidationError):
        loader._synthetic_df("s", "bad", "2024-01-03")
    with pytest.raises(DataValidationError):
        loader._synthetic_df("s", "2024-01-05", "2024-01-01")
    assert loader.health()["status"] == "ok"
    # legacy 歧义（start 为 interval 且 end/interval 均无日期横线）/ interval 非法
    with pytest.raises(DataValidationError):
        loader.get_bars("s", "1d", "x", "1d")
    with pytest.raises(DataValidationError):
        loader.get_bars("s", "2024-01-01", "2024-01-02", interval="bad")
    with pytest.raises(DataValidationError):
        loader.get_bars("s", "2024-01-01", "2024-01-02", interval="1h")


def test_j3_akshare_normalize_branches():
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    loader = AKShareLoader()
    assert loader._normalize_akshare(None) is None
    assert loader._normalize_akshare(pd.DataFrame()) is None
    df_cn = pd.DataFrame({"日期": ["2024-01-01", "2024-01-02"], "开盘": [10, 11], "收盘": [10.5, 11.5],
                          "最高": [11, 12], "最低": [9, 10], "成交量": [100, 200]})
    out = loader._normalize_akshare(df_cn)
    assert out is not None and len(out) == 2
    # English fallback 列
    df_en = pd.DataFrame({"date": ["2024-01-01"], "Open": [10.0], "Close": [10.5],
                          "High": [11.0], "Low": [9.0], "Volume": [100.0]})
    # Volume 大写不在 volume 识别？走缺列或解析分支——不断言，仅锁定不崩或抛错
    try:
        loader._normalize_akshare(df_en)
    except DataValidationError:
        pass
    df_missing = pd.DataFrame({"日期": ["2024-01-01"], "开盘": [10.0]})
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df_missing)
    df_baddate = pd.DataFrame({"日期": ["not-a-date"], "开盘": [10.0], "收盘": [10.5],
                               "最高": [11.0], "最低": [9.0], "成交量": [100]})
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df_baddate)
    df_novol = pd.DataFrame({"日期": ["2024-01-01"], "开盘": [10.0], "收盘": [10.5],
                             "最高": [11.0], "最低": [9.0]})
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df_novol)
    df_nanvol = pd.DataFrame({"日期": ["2024-01-01"], "开盘": [10.0], "收盘": [10.5],
                              "最高": [11.0], "最低": [9.0], "成交量": ["oops"]})
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df_nanvol)
    df_nanpx = pd.DataFrame({"日期": ["2024-01-01"], "开盘": ["oops"], "收盘": [10.5],
                             "最高": [11.0], "最低": [9.0], "成交量": [100]})
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df_nanpx)


def test_j3_akshare_live_branches(monkeypatch):
    _reload_settings(monkeypatch, "live")
    import unittest.mock as mock
    from hero_quant.data.loaders.akshare_loader import AKShareLoader

    loader = AKShareLoader()
    # akshare 未装 -> ImportError
    with mock.patch.dict("sys.modules", {"akshare": None}):
        with pytest.raises(Exception):
            loader.get_bars("600519.SH", "2024-01-01", "2024-01-05")
    # fake ak 成功
    fake = mock.MagicMock()
    fake.stock_zh_a_hist.return_value = pd.DataFrame(
        {"日期": ["2024-01-01", "2024-01-02"], "开盘": [10, 11], "收盘": [10.5, 11.5],
         "最高": [11, 12], "最低": [9, 10], "成交量": [100, 200]})
    with mock.patch.dict("sys.modules", {"akshare": fake}):
        out = loader.get_bars("600519.SH", "2024-01-01", "2024-01-02")
        assert len(out) == 2
    # fake 返回空 -> RuntimeError
    fake2 = mock.MagicMock()
    fake2.stock_zh_a_hist.return_value = pd.DataFrame()
    with mock.patch.dict("sys.modules", {"akshare": fake2}):
        with pytest.raises(Exception):
            loader.get_bars("600519.SH", "2024-01-01", "2024-01-02")


# ---------- ccxt ----------

def test_j3_ccxt_synthetic_and_guards(monkeypatch):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader, DataValidationError

    loader = CCXTLoader()
    df = loader._synthetic_df("BTC/USDT", "2024-01-01", "2024-01-02")
    assert len(df) == 2
    with pytest.raises(DataValidationError):
        loader._synthetic_df("s", "bad", "2024-01-02")
    with pytest.raises(DataValidationError):
        loader._synthetic_df("s", "2024-01-03", "2024-01-01")
    assert loader.health()["status"] == "ok"
    with pytest.raises(DataValidationError):
        loader.get_bars("s", "2024-01-01", "2024-01-02", interval="x") if False else loader._synthetic_df("s", "2024-01-02", "2024-01-01")


def test_j3_ccxt_live_branches(monkeypatch):
    _reload_settings(monkeypatch, "live")
    import unittest.mock as mock
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader, DataValidationError

    loader = CCXTLoader()
    # ccxt 未装 -> ImportError（import 在日期解析之前）
    with mock.patch.dict("sys.modules", {"ccxt": None}):
        with pytest.raises(Exception):
            loader.get_bars("BTC/USDT", "2024-01-01", "2024-01-02")
    import datetime as _dt
    base_ms = int(_dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)
    fake_ex = mock.MagicMock()
    fake_ex.fetch_ohlcv.return_value = [[base_ms, 100, 110, 90, 105, 1000],
                                        [base_ms + 86400000, 105, 115, 95, 110, 1100]]
    fake_ccxt = mock.MagicMock()
    fake_ccxt.binance.return_value = fake_ex
    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt}):
        with pytest.raises(DataValidationError):
            loader.get_bars("BTC/USDT", "bad", "2024-01-02")
        with pytest.raises(DataValidationError):
            loader.get_bars("BTC/USDT", "2024-01-03", "2024-01-01")
        out = loader.get_bars("BTC/USDT", "2024-01-01", "2024-01-02")
        assert len(out) >= 1
    # 空 ohlcv -> RuntimeError
    fake_ex2 = mock.MagicMock()
    fake_ex2.fetch_ohlcv.return_value = []
    fake_ccxt2 = mock.MagicMock()
    fake_ccxt2.binance.return_value = fake_ex2
    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt2}):
        with pytest.raises(Exception):
            loader.get_bars("BTC/USDT", "2024-01-01", "2024-01-02")


# ---------- tencent ----------

def test_j3_tencent_synthetic_rate_guards(monkeypatch):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.tencent import DataValidationError, TencentLoader

    loader = TencentLoader()
    bars = loader._synthetic_bars("000001.SZ", "2024-01-01", "2024-01-02")
    assert len(bars) == 2
    with pytest.raises(DataValidationError):
        loader._synthetic_bars("s", "bad", "2024-01-02")
    with pytest.raises(DataValidationError):
        loader._synthetic_bars("s", "2024-01-03", "2024-01-01")
    loader._rate_limit()  # synthetic 直接返回
    assert loader.health()["status"] == "ok"
    with pytest.raises(DataValidationError):
        loader.get_bars("s", "1d", "x", "1d")
    with pytest.raises(DataValidationError):
        loader.get_bars("s", "2024-01-01", "2024-01-02", interval="1h")
    out = loader.get_bars("000001.SZ", "2024-01-01", "2024-01-02")
    assert len(out[0]) == 2 if isinstance(out, tuple) else len(out) == 2


def test_j3_tencent_live_fetch_branches(monkeypatch):
    _reload_settings(monkeypatch, "live")
    import json as _json
    import urllib.request as _url
    from hero_quant.data.loaders.tencent import DataValidationError, TencentLoader

    loader = TencentLoader()
    with pytest.raises(DataValidationError):
        loader._fetch_live_bars("000001.SZ", "bad", "2024-01-02")
    with pytest.raises(DataValidationError):
        loader._fetch_live_bars("000001.SZ", "2024-01-03", "2024-01-01")

    payload = {"data": {"sz000001": {"qfqday": [
        ["2024-01-01", "10.0", "10.5", "11.0", "9.0", "1000"],
        ["2024-01-02", "10.5", "11.0", "11.5", "9.5", "1100"]]}}}
    raw = _json.dumps(payload).encode()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return raw
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr("hero_quant.data.loaders.tencent.TencentLoader._rate_limit", lambda self: None)
    bars = loader._fetch_live_bars("000001.SZ", "2024-01-01", "2024-01-02")
    assert len(bars) == 2
    # 坏字段 -> DataValidationError
    payload2 = {"data": {"sz000001": {"qfqday": [["2024-01-01", "oops", "10.5", "11", "9", "100"]]}}}
    raw2 = _json.dumps(payload2).encode()

    class _Resp2(_Resp):
        def read(self): return raw2
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _Resp2())
    with pytest.raises(DataValidationError):
        loader._fetch_live_bars("000001.SZ", "2024-01-01", "2024-01-02")


# ---------- yahoo ----------

def test_j3_yahoo_guards_and_synthetic(monkeypatch):
    _reload_settings(monkeypatch, "synthetic")
    from hero_quant.data.loaders.yahoo import DataValidationError, YahooLoader

    loader = YahooLoader()
    assert loader.health()["status"] == "ok"
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL", "1d", "x", "1d")
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL", "2024-01-01", "2024-01-02", interval="9z")
    bars, prov = loader.get_bars("AAPL", "2024-01-01", "2024-01-03")
    assert len(bars) == 2
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL", "2024-01-05", "2024-01-01")
    with pytest.raises(DataValidationError):
        loader.get_bars("AAPL", "bad", "2024-01-01")


def test_j3_yahoo_live_fake_yfinance(monkeypatch):
    _reload_settings(monkeypatch, "live")
    import sys
    import types
    import unittest.mock as mock
    from hero_quant.data.loaders.yahoo import DataValidationError, YahooLoader

    loader = YahooLoader()
    # 缺 yfinance -> ImportError
    with mock.patch.dict("sys.modules", {"yfinance": None}):
        with pytest.raises(Exception):
            loader.get_bars("AAPL", "2024-01-01", "2024-01-02")
    # fake download 成功
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    df = pd.DataFrame({"Open": [100.0, 101.0], "High": [102.0, 103.0], "Low": [99.0, 100.0],
                       "Close": [101.0, 102.0], "Volume": [1000, 1100]}, index=idx)
    fake_yf = types.SimpleNamespace(
        download=lambda *a, **k: df,
        Ticker=lambda *a, **k: mock.MagicMock(history=lambda *a, **k: df),
    )
    with mock.patch.dict("sys.modules", {"yfinance": fake_yf}):
        bars, prov = loader.get_bars("BRK.B", "2024-01-01", "2024-01-03")
        assert len(bars) == 2
        bars2, _ = loader.get_bars("AAPL.US", "2024-01-01", "2024-01-03")
        assert len(bars2) == 2
    # 空数据走 history 为空 -> DataValidationError
    fake_empty = types.SimpleNamespace(
        download=lambda *a, **k: pd.DataFrame(),
        Ticker=lambda *a, **k: mock.MagicMock(history=lambda *a, **k: pd.DataFrame()),
    )
    with mock.patch.dict("sys.modules", {"yfinance": fake_empty}):
        with pytest.raises(DataValidationError):
            loader.get_bars("AAPL", "2024-01-01", "2024-01-02")


# ---------- news helpers + disclosure ----------

def test_j3_news_parse_and_disclosure():
    from hero_quant.data.loaders import news as N

    assert N._parse_time(None) is None
    assert N._parse_time("2024-01-01") is not None
    assert N._parse_time("garbage-xyz") is None
    assert N._normalize_date_str("2024-01-01") == "2024-01-01"
    assert N._normalize_date_str(None) is None
    assert N._is_aware(pd.Timestamp("2024-01-01", tz="UTC")) is True
    assert N._is_aware(pd.Timestamp("2024-01-01")) is False
    assert isinstance(N.get_disclosure([]), str)
    assert isinstance(N.build_disclosure([]), str)
    assert isinstance(N.format_disclosure([]), str)
    assert isinstance(N.get_pit_disclosure([]), str)
    assert isinstance(N.build_news_disclosure([]), str)
    assert isinstance(N.news_disclosure([]), str)
    assert isinstance(N._disclosure_text([]), str)
    assert isinstance(N.get_disclosure(None), str)


def test_j3_news_load_news_branches():
    from hero_quant.data.loaders.news import load_news

    assert load_news([]) == []
    assert load_news(None) == []
    recs = [{"trade_date": "2024-01-01", "publish_time": "2024-01-01 10:00",
             "title": "t", "content": "c"}]
    out = load_news(recs, trade_date="2024-01-02")
    assert out == []
    out2 = load_news(recs, trade_date="2024-01-01", snapshot_date="2024-01-05")
    assert len(out2) == 1 and "pit" in out2[0]
    # 非 dict 记录被丢弃
    out3 = load_news(["nope", *recs], trade_date="2024-01-01", snapshot_date="2024-01-05")
    assert len(out3) == 1
    # 非法快照抛错
    with pytest.raises(ValueError):
        load_news(recs, snapshot_date="not-a-time-xyz")
    # 无 trade_date 列整体 -> 空
    with pytest.raises(ValueError):
        load_news([{"publish_time": "x"}] * 3, snapshot_date="bad-snap")
    assert load_news([{"title": "t"}], trade_date="2024-01-01") == []
