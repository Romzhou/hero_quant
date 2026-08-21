"""Task17 realtime stream Redpanda WS -> streaming factor <200ms — TDD red."""
import time
import pytest


def test_on_tick_hook_exists():
    """BacktestEngine must expose on_tick for streaming incremental factor."""
    from hero_quant.backtest.engine import BacktestEngine

    e = BacktestEngine()
    assert hasattr(e, "on_tick"), "BacktestEngine missing on_tick hook for Task17"
    # must be callable
    assert callable(getattr(e, "on_tick"))


def test_incremental_factor_latency_under_200ms():
    """Incremental factor via WS tick stream must be <200ms per tick (benchmark)."""
    from hero_quant.stream import IncrementalFactor, StreamService
    from hero_quant.backtest.engine import BacktestEngine

    # IncrementalFactor correctness + latency
    fac = IncrementalFactor(window=20)
    # warm up
    for i in range(20):
        fac.update(float(i))

    # benchmark incremental update latency
    latencies = []
    for i in range(100):
        t0 = time.perf_counter()
        val = fac.update(float(i + 20))
        t1 = time.perf_counter()
        lat = (t1 - t0) * 1000  # ms
        latencies.append(lat)
        assert isinstance(val, float)
        assert lat < 200, f"incremental factor latency {lat:.3f}ms >=200ms at tick {i}"

    avg_lat = sum(latencies) / len(latencies)
    assert avg_lat < 50, f"avg latency {avg_lat:.3f}ms too high, expected <50ms"
    p99 = sorted(latencies)[int(len(latencies) * 0.99)]
    assert p99 < 200, f"p99 latency {p99:.3f}ms >=200ms"

    # Engine on_tick must also be <200ms per tick end-to-end
    engine = BacktestEngine()
    tick = {"symbol": "600519.SH", "price": 100.0, "ts": "2026-08-20T09:30:00"}
    t0 = time.perf_counter()
    res = engine.on_tick(tick)
    t1 = time.perf_counter()
    lat_engine = (t1 - t0) * 1000
    assert lat_engine < 200, f"engine.on_tick latency {lat_engine:.3f}ms >=200ms"
    assert "factor" in res or "value" in res or "latency_ms" in res


def test_ws_tick_ingestion():
    """WS tick ingestion via StreamService must accept ticks via sync and async."""
    from hero_quant.stream import StreamService, Tick
    import asyncio

    svc = StreamService(factor_window=5)

    # sync ingest
    tick = Tick(symbol="BTC/USDT", price=50000.0)
    res = svc.ingest_tick(tick)
    assert res is not None
    assert "factor" in res or "value" in res
    assert res.get("latency_ms", 0) < 200

    # async ingest via asyncio queue (WS placeholder)
    async def _async_ingest():
        tick2 = Tick(symbol="BTC/USDT", price=50100.0)
        r = await svc.aingest_tick(tick2)
        return r

    r2 = asyncio.run(_async_ingest())
    assert r2 is not None
    assert r2.get("latency_ms", 0) < 200

    # bulk 50 ticks all <200ms
    for i in range(50):
        t = Tick(symbol="600519.SH", price=100 + i * 0.1)
        t0 = time.perf_counter()
        rr = svc.ingest_tick(t)
        lat = (time.perf_counter() - t0) * 1000
        assert lat < 200
        assert rr.get("latency_ms", lat) < 200


def test_redpanda_placeholder():
    """Redpanda placeholder publish/consume must not error and be present."""
    from hero_quant.stream import StreamService, Tick

    svc = StreamService(redpanda_config={"bootstrap_servers": "localhost:9092"})
    # placeholder api must exist
    assert hasattr(svc, "publish_to_redpanda") or hasattr(svc, "publish")
    assert hasattr(svc, "consume_from_redpanda") or hasattr(svc, "consume")

    # publish should be no-op placeholder but not raise
    tick = Tick(symbol="600519.SH", price=101.0)
    # try both naming conventions
    pub = getattr(svc, "publish_to_redpanda", None) or getattr(svc, "publish", None)
    con = getattr(svc, "consume_from_redpanda", None) or getattr(svc, "consume", None)

    res_pub = pub(tick)
    # consume returns list or deque; may be empty initially
    res_con = con(limit=1)
    assert isinstance(res_con, list)


def test_incremental_correctness_vs_full_sma():
    """Incremental SMA must match full SMA within tolerance."""
    from hero_quant.stream import IncrementalFactor
    import pandas as pd
    from hero_quant.quantlib.indicators import sma

    window = 5
    fac = IncrementalFactor(window=window)
    prices = [10, 11, 12, 13, 14, 15, 16, 17]

    for price in prices:
        fac.update(price)

    # incremental factor should equal pandas rolling sma on same window
    s = pd.Series(prices, dtype=float)
    expected = float(sma(s, window).iloc[-1])
    incremental = fac.value
    assert incremental == pytest.approx(expected, rel=1e-6)
