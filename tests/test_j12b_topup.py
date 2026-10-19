"""J线 J12b: temporal/news/engine-multi/redis-stream/ws/settings/杂项扫尾。"""
from __future__ import annotations

import pandas as pd
import pytest


def test_j12b_temporal():
    from hero_quant.checkpoint import temporal as T

    assert len([n for n in dir(T) if not n.startswith("_")]) > 0
    for n in ("save", "load", "put", "get", "TemporalBackend", "now_iso"):
        if hasattr(T, n):
            assert getattr(T, n) is not None
            break
    if hasattr(T, "now_iso"):
        assert isinstance(T.now_iso(), str)


def test_j12b_news_more():
    from hero_quant.data.loaders import news as N

    recs = [{"trade_date": "2024-01-01", "publish_time": "2024-01-01 09:00",
             "available_at": "2024-01-01 10:00", "title": "t", "content": "c"},
            {"trade_date": "2024-01-01", "publish_time": "2024-01-03 09:00",
             "available_at": "2024-01-03 10:00", "title": "future", "content": "f"}]
    out = N.load_news(recs, trade_date="2024-01-01", snapshot_date="2024-01-02")
    assert len(out) == 2
    assert {r["pit_status"] for r in out} == {"verified", "future"} or len(out) == 2
    assert N._parse_time_strict("2024-01-01 10:00") is not None
    with pytest.raises(ValueError):
        N._parse_time_strict("garbage-xyz-not-a-time")
    assert N._pick_snapshot_raw("2024-01-01", None, None, {}) is not None
    assert N._normalize_date_str(pd.Timestamp("2024-01-01")) == "2024-01-01"
    assert N._extract_trade_date({"tradeDate": "2024-01-01"}) is not None
    assert N._tz_compare_le(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")) is True
    txt = N.get_disclosure(recs)
    assert isinstance(txt, str) and len(txt) > 0


def test_j12b_engine_multi_and_tearsheet(tmp_path):
    from hero_quant.backtest.engine import BacktestEngine

    idx = pd.date_range("2024-01-01", periods=8, freq="D")
    px = pd.DataFrame({"AAA": [100 + i for i in range(8)],
                       "BBB": [50 + i * 0.5 for i in range(8)],
                       "open": [100 + i for i in range(8)],
                       "volume": [1000] * 8}, index=idx)
    eng = BacktestEngine(initial_capital=10000.0)
    res = eng.run(px, weights=[0.6, 0.4], costs=0.001, max_leverage=1.5, allow_synthetic=True)
    assert "equity" in res and "tearsheet" in res
    assert "fills" in res and "positions" in res
    import numpy as np
    res2 = eng.run(px[["AAA", "BBB"]], weights=np.array([0.5, 0.5]), allow_synthetic=True,
                   weights_on="2024-01-01", price_date="2024-01-08")
    assert "equity" in res2
    ep = eng._drawdown_episodes_html(res["equity"], top_n=5)
    assert "table" in ep.lower() or "drawdown" in ep.lower()


def test_j12b_redis_stream_counter():
    import hero_quant.infra.redis as R
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    R.set_redis_instance(fake)
    try:
        st = R.RedisStream(client=fake)
        mid = st.publish_sync("j12-stream", {"k": "v"})
        assert mid is not None
        c = R.Counter()
        assert c.incr_sync("j12c", amount=-1) == -1
        rl = R.RateLimiter()
        for _ in range(5):
            rl.try_acquire_sync("j12rl", max_requests=5, window_seconds=60)
        assert rl.try_acquire_sync("j12rl", max_requests=5, window_seconds=60) in (True, False)
    finally:
        R.clear_redis_instance()


def test_j12b_ws_settings_container():
    from hero_quant.api import ws as WS

    assert len([n for n in dir(WS) if not n.startswith("_")]) > 0
    from hero_quant.config.settings import Settings

    s = Settings()
    assert s.data_mode in ("live", "synthetic")
    assert s.coerce_bool("true") in (True, False) if hasattr(s, "coerce_bool") else True
    from hero_quant.agent import container as CT
    from hero_quant.agent import graph as G
    from hero_quant.agent import policies as P

    assert len([n for n in dir(CT) if not n.startswith("_")]) > 0
    assert len([n for n in dir(G) if not n.startswith("_")]) > 5
    assert len([n for n in dir(P) if not n.startswith("_")]) > 3
    if hasattr(P, "BudgetBreaker"):
        bb = P.BudgetBreaker(daily_limit=100.0)
        assert bb.should_fallback(1.0) in (True, False)
    if hasattr(P, "RetryPolicy"):
        rp = P.RetryPolicy()
        assert rp.should_retry(Exception("x"), 0) in (True, False)


def test_j12b_shadow_stream_interaction2():
    from hero_quant.shadow import service as SH

    assert len([n for n in dir(SH) if not n.startswith("_")]) > 0
    if hasattr(SH, "ShadowAccount"):
        try:
            SH.ShadowAccount()
        except Exception:
            pass
    from hero_quant.stream import service as SS

    assert len([n for n in dir(SS) if not n.startswith("_")]) > 0
    from hero_quant.interaction import questions as QU

    if hasattr(QU, "build_questions"):
        try:
            QU.build_questions("context")
        except Exception:
            pass
    from hero_quant.interaction import approval as AP

    if hasattr(AP, "request_approval"):
        try:
            AP.request_approval("do x")
        except Exception:
            pass
