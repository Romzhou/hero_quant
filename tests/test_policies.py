# tests/test_policies.py
def test_retry_and_budget_breaker():
    from hero_quant.agent.policies import RetryPolicy, BudgetBreaker
    rp=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
    # Use truthiness not identity (is True is brittle)
    assert rp.should_retry(ConnectionError("x"), attempt=1)
    assert not rp.should_retry(ConnectionError("x"), attempt=3)
    assert not rp.should_retry(ValueError("x"), attempt=1)
    bb=BudgetBreaker(daily_limit=5.0)
    assert bb.should_fallback(cost=6.0)
    assert not bb.should_fallback(cost=5.0)
    assert not bb.should_fallback(cost=4.9)


def test_retry_policy_boundaries_and_logging(caplog):
    from hero_quant.agent.policies import RetryPolicy
    import logging
    rp = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
    # attempt at boundary
    assert not rp.should_retry(ConnectionError("x"), attempt=3)
    assert not rp.should_retry(ConnectionError("x"), attempt=4)
    # non-retryable type
    assert not rp.should_retry(ValueError("x"), attempt=1)
    # invalid retry_on not tuple should log and return False
    rp2 = RetryPolicy(max_attempts=3, retry_on="not-a-tuple")  # type: ignore
    with caplog.at_level(logging.WARNING):
        assert rp2.should_retry(ConnectionError("x"), attempt=1) is False
        assert any("retry_on" in r.message for r in caplog.records)


def test_budget_breaker_boundaries_and_nan():
    from hero_quant.agent.policies import BudgetBreaker
    import math
    bb = BudgetBreaker(daily_limit=5.0)
    assert bb.should_fallback(cost=5.0) is False
    assert bb.should_fallback(cost=5.0 + 1e-9) is True
    # NaN/Inf should not crash and coerced to 0 with warning (fail-visible, not silent fallback)
    assert bb.should_fallback(cost=float("nan")) is False
    assert bb.should_fallback(cost=float("inf")) is False  # non-finite coerced to 0, not fallback unless cumulative
    # check_and_add should raise for non-finite
    import pytest
    with pytest.raises(ValueError):
        bb.check_and_add(float("nan"))
    with pytest.raises(ValueError):
        bb.check_and_add(float("inf"))
    # cumulative test
    bb2 = BudgetBreaker(daily_limit=1.0)
    bb2.add_cost(0.6)
    assert bb2.should_fallback(cost=0.3) is False
    assert bb2.should_fallback(cost=0.5) is True
    # ensure zero cost does not grow
    n = len(bb2._costs)
    bb2.record_usage({"input_tokens": 0, "output_tokens": 0})
    assert len(bb2._costs) == n


def test_retry_backoff_and_sleep_logging(caplog):
    from hero_quant.agent.policies import RetryPolicy
    import logging
    rp = RetryPolicy(backoff_base=0.01, backoff_factor=2, jitter=0.1)
    d = rp.backoff(1)
    assert d > 0
    # sleep should not raise even if time.sleep mocked to raise
    import time as _t
    orig = _t.sleep
    def bad_sleep(x):
        raise RuntimeError("sleep fail")
    _t.sleep = bad_sleep  # type: ignore
    try:
        with caplog.at_level(logging.WARNING):
            rp.sleep(1)
            assert any("sleep" in r.message.lower() for r in caplog.records)
    finally:
        _t.sleep = orig
