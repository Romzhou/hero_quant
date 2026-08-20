def test_trace_concurrent_10_threads(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    import threading, json
    w = TraceWriter(tmp_path, sidecar_threshold=50000)
    def worker(n):
        for i in range(100): w.append({"type":"tool_result","tool":"t","content":f"x{n}-{i}"})
    ths=[threading.Thread(target=worker, args=(n,)) for n in range(10)]
    [t.start() for t in ths]; [t.join() for t in ths]
    lines=(tmp_path/"trace.jsonl").read_text().strip().splitlines()
    assert len(lines)==1000
    assert all(json.loads(l) for l in lines)

def test_backtest_throughput():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd, time
    prices=pd.DataFrame({"close": list(range(1000,1100))}, index=pd.date_range("2026-08-01", periods=100))
    eng=BacktestEngine(); t0=time.perf_counter(); res=eng.run(prices, weights=[0.5,0.5])
    assert res["metrics"]["sharpe"] is not None
    assert time.perf_counter()-t0 < 2.0

def test_circuit_breaker_threshold():
    from hero_quant.telemetry.circuit import CircuitBreaker
    cb=CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
    for _ in range(5): cb.record_failure()
    assert cb.state=="OPEN"
