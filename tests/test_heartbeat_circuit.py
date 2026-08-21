def test_heartbeat_and_circuit():
    from hero_quant.telemetry.heartbeat import HeartbeatTimer
    import time

    fired = []
    with HeartbeatTimer("t", interval=0.1, emit=lambda e: fired.append(e)):
        time.sleep(0.35)
    assert len(fired) >= 2
    from hero_quant.telemetry.circuit import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
    for _ in range(5):
        cb.record_failure()
    assert cb.state == "OPEN"


def test_circuit_half_open_5_probes():
    """Half-open must require >= half_open_max_calls(5) continuous successes before CLOSED else re-OPEN."""
    import time

    from hero_quant.telemetry.circuit import CircuitBreaker

    # trigger OPEN with minimal failures (window large so events not pruned)
    cb = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=0.05, half_open_max_calls=5)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "OPEN"

    # wait for OPEN -> HALF_OPEN transition
    time.sleep(0.07)
    assert cb.state == "HALF_OPEN"
    # allow should permit probes up to max
    assert cb.allow() is True

    # 1 probe must NOT close (old bug closed after 1)
    cb.record_success()
    assert cb.state == "HALF_OPEN", "should remain HALF_OPEN after 1 probe, requires 5"
    assert cb.allow() is True

    # 2,3,4 probes still HALF_OPEN
    cb.record_success()
    assert cb.state == "HALF_OPEN"
    cb.record_success()
    assert cb.state == "HALF_OPEN"
    cb.record_success()
    assert cb.state == "HALF_OPEN", "should remain HALF_OPEN after 4 probes, requires 5"

    # 5th probe with continuous success rate ok -> CLOSED
    cb.record_success()
    assert cb.state == "CLOSED", "should be CLOSED after 5 continuous success probes"
    assert cb.is_closed() is True

    # verify re-OPEN on failure during half-open
    cb2 = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=0.05, half_open_max_calls=5)
    for _ in range(2):
        cb2.record_failure()
    assert cb2.state == "OPEN"
    time.sleep(0.07)
    assert cb2.state == "HALF_OPEN"
    cb2.record_success()
    assert cb2.state == "HALF_OPEN"
    # failure during half-open must re-OPEN immediately
    cb2.record_failure()
    assert cb2.state == "OPEN", "failure during HALF_OPEN must re-OPEN"
    assert cb2.is_open() is True
    # after re-OPEN, allow should be False until half-open again
    assert cb2.allow() is False

    # else re-OPEN when success rate not met after 5 probes (slow bucket)
    cb3 = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=0.05, half_open_max_calls=5)
    for _ in range(2):
        cb3.record_failure()
    assert cb3.state == "OPEN"
    time.sleep(0.07)
    assert cb3.state == "HALF_OPEN"
    for _ in range(5):
        cb3.record_success(duration=31)  # slow -> slow_rate high
    # after 5 slow probes, rates exceed threshold -> re-OPEN
    assert cb3.state == "OPEN", "should re-OPEN after 5 probes with high slow rate"
