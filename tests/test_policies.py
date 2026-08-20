# tests/test_policies.py
def test_retry_and_budget_breaker():
    from hero_quant.agent.policies import RetryPolicy, BudgetBreaker
    rp=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
    assert rp.should_retry(ConnectionError("x"), attempt=1) is True
    bb=BudgetBreaker(daily_limit=5.0)
    assert bb.should_fallback(cost=6.0) is True
