import pytest
from hero_quant.governance.wall_time import WallTimeBudget, WallTimeExceeded, _resolve_default_budget

def test_time_call_does_not_swallow_original():
    b = WallTimeBudget(budget_seconds=0.01, operation="test")
    try:
        with b:
            raise ValueError("original")
        assert False
    except ValueError as e:
        assert "original" in str(e)
    except WallTimeExceeded:
        assert False, "should not swallow ValueError"

def test_invalid_budget_raises():
    try:
        _resolve_default_budget("abc")
        assert False
    except ValueError:
        pass

def test_resolve_default_budget_narrow():
    from hero_quant.governance.wall_time import _resolve_default_budget
    import pytest
    # explicit bad value should raise ValueError (fail-visible)
    with pytest.raises((ValueError, TypeError)):
        _resolve_default_budget(explicit="bad_value")
    # valid budgets return float
    assert _resolve_default_budget(explicit=30.0) == 30.0
