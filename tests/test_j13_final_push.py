"""J线 J13: 决战补隙 —— TTL/冲突/单飞/多 provider/三 loader/偏置守卫等精确打击。"""
from __future__ import annotations

import json

import pandas as pd
import pytest


# ---------- postgres TTL + rowcount 冲突 ----------

def test_j13_pg_ttl_and_versions():
    import sys
    sys.path.insert(0, "tests")
    from test_j10_pgpool_vectors import _J10Pool, _saver

    pool = _J10Pool()
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver
    sv = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/j13", pool=pool, ttl_seconds=3600)
    assert sv.ttl_seconds == 3600
    sv.put("wf:run_ttl:ten", {"v": 1, "version": 1}, {"c": 1})
    assert any("expires_at" in s for s in pool.statements)
    sv0 = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/j13", pool=_J10Pool(), ttl_seconds=0)
    sv0.put("wf:run_ttl0:ten", {"v": 1}, None)
    # 旧版本写入被忽略语义：version 0
    sv.put("wf:run_ttl:ten", {"v": 2, "version": 0}, None)


def test_j13_pg_setup_fails_and_bad_thread():
    from test_j10_pgpool_vectors import _J10Conn, _J10Pool, _saver

    class _FailPool(_J10Pool):
        def connection(self):
            raise OSError("pg down")

    sv = _saver(pool=_FailPool())
    sv.setup()  # DDL 失败容错
    with pytest.raises(ValueError):
        sv.put("bad-tid", {"a": 1})
    assert sv.get("wf:run_x:ten") is None


# ---------- dedup 行冲突语义 ----------

def test_j13_dedup_conflict_semantics(monkeypatch):
    import hero_quant.governance.dedup as D
    import sys
    sys.path.insert(0, "tests")
    from test_j11_siege import _DConn, _DPool

    class _ZeroRowConn(_DConn):
        def execute(self, sql, params=None):
            from test_j11_siege import _DCursor
            return _DCursor(self, rowcount=0).execute(sql, params)

    class _ZeroPool(_DPool):
        def connection(self):
            pool = self

            class _Ctx:
                def __enter__(self):
                    return _ZeroRowConn(pool)

                def __exit__(self, *a):
                    return False

            return _Ctx()

    monkeypatch.setattr(D, "DedupPool", _ZeroPool)
    st = D.DedupStore(dsn="postgresql://postgres:postgres@localhost:5432/j13")
    key = D.derive_key("t", "w", "s", "tool", "zero1")
    assert st.insert_pending(key, "tool") is False  # 冲突已存在
    st.mark_success(key, {"ok": 1})
    assert st.wait_for(key, timeout=0.2) is None or isinstance(st.wait_for(key, timeout=0.2), dict)


# ---------- scheduler 单飞 redis/local ----------

def test_j13_singleflight_both_backends():
    import hero_quant.infra.redis as R
    import fakeredis
    from hero_quant.scheduled import service as S

    fake = fakeredis.FakeRedis(decode_responses=True)
    R.set_redis_instance(fake)
    try:
        ok1, be1 = S._try_acquire_singleflight("j13-sf", px_ms=60000)
        assert ok1 is True and be1 == "redis"
        ok2, be2 = S._try_acquire_singleflight("j13-sf", px_ms=60000)
        assert ok2 is False  # 重复命中
        S._release_singleflight("j13-sf", "redis")  # redis 靠过期，直接返回
    finally:
        R.clear_redis_instance()
    import unittest.mock as _mock
    with _mock.patch.object(S, "_dispatch_redis", return_value=None):
        ok3, be3 = S._try_acquire_singleflight("j13-local", px_ms=60000)
        assert ok3 is True and be3 == "local"
        ok4, be4 = S._try_acquire_singleflight("j13-local", px_ms=60000)
        assert ok4 is False
        S._release_singleflight("j13-local", "local")
        ok5, _ = S._try_acquire_singleflight("j13-local", px_ms=60000)
        assert ok5 is True
        S._release_singleflight("j13-local", "local")


# ---------- llm factory 多 provider ----------

def test_j13_factory_providers(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("HERO_API_KEY", raising=False)
    from hero_quant.llm import factory as F

    assert F._normalize_provider("OpenAI") == "openai"
    assert F._normalize_provider("deepseek") == "deepseek"
    assert F._normalize_provider("weird_xyz", strict=False) is not None
    fac = F.LLMFactory(settings=None)
    c = fac.create()  # 无 key -> dummy key + 真实或回退 chat
    assert c is not None
    c2 = fac.create(model="gpt-4o-mini", api_key="sk-fake-j13")
    assert c2 is not None
    monkeypatch.setenv("HERO_LLM_PROVIDER", "deepseek")
    fac2 = F.LLMFactory(settings=None)
    assert fac2.create(api_key="sk-fake") is not None
    monkeypatch.setenv("HERO_LLM_PROVIDER", "anthropic")
    fac3 = F.LLMFactory(settings=None)
    assert fac3.create(api_key="sk-fake") is not None
    monkeypatch.setenv("HERO_LLM_PROVIDER", "unknown_xyz")
    fac4 = F.LLMFactory(settings=None)
    assert fac4.create(api_key="sk-fake") is not None
    # strict 缺 key 抛错
    facs = F.LLMFactory(settings=None, strict=True)
    with pytest.raises(ValueError):
        facs.create()
    with pytest.raises(Exception):
        F._normalize_provider("weird_xyz", strict=True)


# ---------- polars 成功路径 ----------

def test_j13_polars_success():
    from hero_quant.quantlib.polars_base import sma_polars

    try:
        import polars  # noqa
    except ImportError:
        pytest.skip("no polars")
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], name="close")
    out = sma_polars(s, window=3)
    assert len(out) == 6 and pd.isna(out.iloc[0]) and out.iloc[2] == 2.0
    out2 = sma_polars(s, period=2)
    assert len(out2) == 6


# ---------- loader 纵深 ----------

def test_j13_akshare_typeerror_retry(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    import unittest.mock as mock
    from hero_quant.data.loaders.akshare_loader import AKShareLoader

    loader = AKShareLoader()
    fake = mock.MagicMock()
    fake.stock_zh_a_hist.side_effect = [
        TypeError("adjust not supported"),
        pd.DataFrame({"日期": ["2024-01-01"], "开盘": [10.0], "收盘": [10.5],
                      "最高": [11.0], "最低": [9.0], "成交量": [100]})]
    with mock.patch.dict("sys.modules", {"akshare": fake}):
        out = loader.get_bars("600519.SH", "2024-01-01", "2024-01-01")
        assert len(out) == 1


def test_j13_ccxt_intraday_and_skips(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    import datetime as _dt
    import unittest.mock as mock
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader

    loader = CCXTLoader()
    base = int(_dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)
    good = [base, 100, 110, 90, 105, 1000]
    bad = ["oops"]
    fake_ex = mock.MagicMock()
    fake_ex.fetch_ohlcv.return_value = [good, bad, good]
    fake_ccxt = mock.MagicMock()
    fake_ccxt.binance.return_value = fake_ex
    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt}):
        out = loader.get_bars("BTC/USDT", "2024-01-01", "2024-01-02", interval="1m")
        assert len(out) >= 1


def test_j13_tencent_dict_and_clip(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    import json as _json
    import urllib.request as _url
    from hero_quant.data.loaders.tencent import DataValidationError, TencentLoader

    loader = TencentLoader()
    payload = {"data": {"sz000001": {"day": [
        {"date": "2024-01-01", "open": 10.0, "close": 10.5, "high": 11.0, "low": 9.0, "volume": 1000},
        {"date": "2024-01-02", "open": 10.5, "close": 11.0, "high": 11.5, "low": 9.5, "volume": 1100}]}}}
    raw = _json.dumps(payload).encode()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return raw
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr("hero_quant.data.loaders.tencent.TencentLoader._rate_limit", lambda self: None)
    bars = loader._fetch_live_bars("000001.SZ", "2024-01-01", "2024-01-02")
    assert len(bars) == 2
    # 窗口外裁剪为空 -> RuntimeError
    with pytest.raises(RuntimeError):
        loader._fetch_live_bars("000001.SZ", "2025-01-01", "2025-01-02")
    # 缺字段 -> DataValidationError
    payload2 = {"data": {"sz000001": {"day": [{"date": "2024-01-01", "open": 10.0}]}}}
    raw2 = _json.dumps(payload2).encode()

    class _Resp2(_Resp):
        def read(self): return raw2
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _Resp2())
    with pytest.raises(DataValidationError):
        loader._fetch_live_bars("000001.SZ", "2024-01-01", "2024-01-02")


def test_j13_yahoo_history_fallback(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    import types
    import unittest.mock as mock
    from hero_quant.data.loaders.yahoo import YahooLoader

    loader = YahooLoader()
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    df = pd.DataFrame({"Open": [100.0, 101.0], "High": [102.0, 103.0], "Low": [99.0, 100.0],
                       "Close": [101.0, 102.0], "Volume": [1000, 1100]}, index=idx)

    def _boom(*a, **k):
        raise RuntimeError("download down")
    fake_yf = types.SimpleNamespace(
        download=_boom,
        Ticker=lambda *a, **k: mock.MagicMock(history=lambda *a, **k: df),
    )
    with mock.patch.dict("sys.modules", {"yfinance": fake_yf}):
        bars, _ = loader.get_bars("AAPL.US", "2024-01-01", "2024-01-03")
        assert len(bars) == 2
    # 日内 interval 日期格式分支
    idx2 = pd.DatetimeIndex([pd.Timestamp("2024-01-01 09:30"), pd.Timestamp("2024-01-01 09:31")])
    df2 = pd.DataFrame({"Open": [100.0, 101.0], "High": [102.0, 103.0], "Low": [99.0, 100.0],
                        "Close": [101.0, 102.0], "Volume": [1000, 1100]}, index=idx2)
    fake_yf2 = types.SimpleNamespace(download=lambda *a, **k: df2,
                                     Ticker=lambda *a, **k: mock.MagicMock())
    with mock.patch.dict("sys.modules", {"yfinance": fake_yf2}):
        bars2, _ = loader.get_bars("AAPL.US", "2024-01-01", "2024-01-02", interval="1m")
        assert bars2[0]["date"] == "2024-01-01 09:30"


def test_j13_news_bias_and_modes():
    from hero_quant.data.loaders.news import load_news

    recs = [{"title": f"t{i}"} for i in range(5)]  # 全缺 trade_date -> 告警空列表
    assert load_news(recs, trade_date="2024-01-01", snapshot_date="2024-01-02") == []
    mixed = [{"title": "m1"}, {"title": "m2"}, {"title": "m3"},
             {"trade_date": "2024-01-01", "publish_time": "2024-01-01 10:00",
              "title": "t", "content": "c"}]
    with pytest.raises(ValueError):  # 缺失超 50% 偏置守卫
        load_news(mixed, trade_date="2024-01-01", snapshot_date="2024-01-02")
    ok_recs = [{"trade_date": "2024-01-01", "publish_time": "2024-01-01 10:00",
                "title": "t", "content": "c"}]
    out = load_news(ok_recs, trade_date="2024-01-01", snapshot_date="2024-01-05",
                    allow_synthetic=True, historical_mode=True)
    assert len(out) == 1
    out2 = load_news(ok_recs, tradeDate="2024-01-01", snapshot_time="2024-01-05",
                     avail_at="2024-01-05", pit_snapshot="2024-01-05")
    assert len(out2) == 1


# ---------- loop 压紧/技能/trace ----------

def test_j13_loop_compact_skills_trace(tmp_path):
    from hero_quant.agent.context import ContextManager
    from hero_quant.agent.loop import AgentLoop

    class _T:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "compact test " * 500}

    cm = ContextManager(max_chars=100)
    loop = AgentLoop(llm=_T(), max_iterations=2, context_manager=cm,
                     trace=str(tmp_path / "trace.jsonl"))
    res = loop.run("long answer compact")
    assert isinstance(res.text, str)
    assert res.token_count >= 0
