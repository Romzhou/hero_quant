def test_heartbeat_and_circuit():
    from hero_quant.telemetry.heartbeat import HeartbeatTimer
    import threading, time
    fired=[]
    with HeartbeatTimer("t", interval=0.1, emit=lambda e: fired.append(e)):
        time.sleep(0.35)
    assert len(fired)>=2
    from hero_quant.telemetry.circuit import CircuitBreaker
    cb=CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
    for _ in range(5): cb.record_failure()
    assert cb.state=="OPEN"
