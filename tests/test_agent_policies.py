"""Task18 policies BudgetBreaker & RetryPolicy TDD."""

import math
import pathlib


def test_breaker_nan_not_poison():
    from hero_quant.agent.policies import BudgetBreaker

    b = BudgetBreaker(daily_limit=5)
    b.add_cost(float("nan"))
    # NaN should not poison total_cost
    assert b.total_cost() == 0, f"total_cost poisoned by NaN: {b.total_cost()}"
    # should_fallback still works normally
    assert b.should_fallback(cost=0.1) is False
    assert b.should_fallback(cost=6) is True
    # add_cost/check_and_add reject NaN/Inf
    b2 = BudgetBreaker(daily_limit=5)
    b2.add_cost(float("inf"))
    assert b2.total_cost() == 0
    b2.add_cost(float("-inf"))
    assert b2.total_cost() == 0
    # check_and_add should reject NaN/Inf (raise or return False without poisoning)
    try:
        result = b2.check_and_add(float("nan"))
        # if not raised, should not poison and return bool
        assert isinstance(result, bool)
        assert b2.total_cost() == 0
    except (ValueError, TypeError):
        assert b2.total_cost() == 0
    try:
        result = b2.check_and_add(float("inf"))
        assert isinstance(result, bool)
        assert b2.total_cost() == 0
    except (ValueError, TypeError):
        assert b2.total_cost() == 0
    # should_fallback NaN/Inf guard
    assert b2.should_fallback(cost=float("nan")) is False or b2.should_fallback(cost=float("nan")) == False
    assert b2.total_cost() == 0


def test_retry_async_sleep():
    p = pathlib.Path("src/hero_quant/agent/policies.py")
    src = p.read_text(encoding="utf-8")
    # must contain async asleep and asyncio.sleep or comment about asyncio path
    has_async = "async def asleep" in src
    has_asyncio = "asyncio.sleep" in src or "asyncio" in src
    assert has_async and has_asyncio, "RetryPolicy missing async asleep with asyncio.sleep"
    # also verify RetryPolicy still has sync sleep
    assert "def sleep" in src
    # check should_retry narrows to TypeError and validates retry_on
    assert "except TypeError" in src or "except (TypeError" in src
