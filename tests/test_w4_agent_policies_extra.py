"""W4 覆盖率补齐：agent/policies.py 的异常兜底分支。

覆盖目标（src/hero_quant/agent/policies.py）：
- error_handler：LG Command 构造失败时回落为 dict
- RetryPolicy.backoff：抖动计算抛异常时回落为 0 抖动
- RetryPolicy.asleep：asyncio.sleep 抛异常时只告警
- BudgetBreaker.record_usage：estimate_cost 抛异常时回落 0.0
- BudgetBreaker.add_cost：非有限值忽略、非法值忽略
- BudgetBreaker.should_fallback：累计窗口剪枝后不再触发

断言均为具体数值/返回类型/异常类型，不使用弱断言。
"""

from __future__ import annotations

import pytest

from hero_quant.agent import policies as pol
from hero_quant.agent.policies import BudgetBreaker, RetryPolicy, error_handler


def test_error_handler_falls_back_to_dict_when_command_construction_fails(monkeypatch):
    """Command 构造失败时回落为 {"goto": ..., "update": ...} 字典。"""

    class _BadCommand:
        def __init__(self, **kwargs):
            raise RuntimeError("cannot build command")

    monkeypatch.setattr(pol, "_get_lg_command", lambda: _BadCommand)
    out = error_handler({}, ValueError("boom"))
    assert isinstance(out, dict)
    assert out["goto"] == "compensate"
    assert out["update"]["error"] == "boom"


def test_error_handler_uses_real_command_when_available():
    """默认路径返回带 goto=compensate 的 Command 对象。"""
    cmd = error_handler({}, ValueError("boom"))
    assert getattr(cmd, "goto", None) == "compensate"


def test_backoff_jitter_failure_falls_back_to_zero(monkeypatch):
    """抖动计算抛异常时退化为 0 抖动（仍返回基础退避）。"""
    monkeypatch.setattr(pol.random, "uniform", lambda a, b: (_ for _ in ()).throw(RuntimeError("no rng")))
    p = RetryPolicy(backoff_base=2.0, backoff_factor=1.0, jitter=0.5)
    assert p.backoff(1) == pytest.approx(2.0)


def test_asleep_swallows_exception(monkeypatch):
    """asyncio.sleep 抛异常时只告警不抛。"""

    async def _boom(_d):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(pol.asyncio, "sleep", _boom)
    p = RetryPolicy(backoff_base=0.0, jitter=0.0)
    import asyncio

    asyncio.run(p.asleep(1))  # 不抛


def test_record_usage_propagates_estimate_cost_failure(monkeypatch):
    """estimate_cost 抛异常时向上传播（record_usage 自身不静默吞错；调用方负责兜底）。"""
    b = BudgetBreaker(daily_limit=10.0)

    def _boom(_usage):
        raise RuntimeError("broken")

    monkeypatch.setattr(b, "estimate_cost", _boom)
    with pytest.raises(RuntimeError, match="broken"):
        b.record_usage({"input_tokens": 100})
    assert b.total_cost() == 0.0


def test_add_cost_ignores_non_finite_and_invalid():
    """add_cost 忽略 NaN/Inf/非法字符串。"""
    b = BudgetBreaker(daily_limit=10.0)
    b.add_cost(float("nan"))
    b.add_cost(float("inf"))
    b.add_cost("abc")
    b.add_cost(None)
    assert b.total_cost() == 0.0


def test_should_fallback_respects_pruned_window(monkeypatch):
    """窗口外的历史成本被剪除后不再触发熔断。"""
    b = BudgetBreaker(daily_limit=5.0, window_seconds=1)
    b.add_cost(4.0)
    real_monotonic = pol.time.monotonic
    monkeypatch.setattr(pol.time, "monotonic", lambda: real_monotonic() + 10)
    assert b.should_fallback(cost=1.0) is False


def test_should_fallback_with_zero_limit_and_zero_cost():
    """daily_limit=0 且 cost=0 时不触发（0 > 0 为假）。"""
    b = BudgetBreaker(daily_limit=0.0)
    assert b.should_fallback(cost=0.0) is False


def test_check_and_add_appends_even_when_fallback_triggered():
    """check_and_add 触发熔断时仍记录本次成本。"""
    b = BudgetBreaker(daily_limit=1.0)
    assert b.check_and_add(2.0) is True
    assert b.total_cost() == pytest.approx(2.0)


def test_budget_breaker_prune_keeps_recent_costs():
    """窗口内的成本被保留。"""
    b = BudgetBreaker(daily_limit=100.0, window_seconds=3600)
    b.add_cost(1.0)
    b.add_cost(2.0)
    assert b.total_cost() == pytest.approx(3.0)


def test_retry_policy_should_retry_with_empty_retry_on_tuple():
    """retry_on 为空 tuple 时不重试（无类型可匹配）。"""
    p = RetryPolicy(max_attempts=3, retry_on=())
    assert p.should_retry(ValueError("x"), 0) is False


def test_retry_policy_max_attempts_zero_never_retries():
    """max_attempts=0 时不重试。"""
    p = RetryPolicy(max_attempts=0)
    assert p.should_retry(ValueError("x"), 0) is False
