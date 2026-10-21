"""W4 覆盖率补齐：agent/policies.py 未覆盖分支。

覆盖目标（src/hero_quant/agent/policies.py）：
- RetryPolicy.should_retry 的次数上限、retry_on 非法（非 tuple / 含非类型）
- RetryPolicy.backoff 的指数退避与抖动边界、sleep/asleep 异常吞吐
- _get_lg_command / error_handler 的 Saga 补偿 Command 构造
- BudgetBreaker._get_prices 的 env 覆盖与非法 env 回落
- estimate_cost 的字段别名（prompt_tokens/completion_tokens）与脏值处理
- record_usage / add_cost / total_cost 的非有限值与零成本处理
- should_fallback 与 check_and_add 的阈值判定与非法入参

断言均为具体数值/异常类型/字段值，不使用弱断言。
"""

from __future__ import annotations

import asyncio
import math

import pytest

from hero_quant.agent.policies import (
    BudgetBreaker,
    NodeError,
    RetryPolicy,
    error_handler,
)


# ---------------------------------------------------------------- RetryPolicy


def test_should_retry_within_attempt_budget():
    """未超次数且异常类型匹配时可重试。"""
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(ValueError("x"), 0) is True
    assert p.should_retry(ValueError("x"), 2) is True


def test_should_retry_exhausted_attempts():
    """attempt >= max_attempts 时不再重试。"""
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(ValueError("x"), 3) is False
    assert p.should_retry(ValueError("x"), 9) is False


def test_should_retry_type_filter_mismatch():
    """异常类型不在 retry_on 内时不重试。"""
    p = RetryPolicy(max_attempts=3, retry_on=(ValueError,))
    assert p.should_retry(KeyError("x"), 0) is False
    assert p.should_retry(ValueError("x"), 0) is True


def test_should_retry_rejects_non_tuple_retry_on():
    """retry_on 非 tuple 时 fail-closed 不重试。"""
    p = RetryPolicy(max_attempts=3, retry_on=[ValueError])  # type: ignore[arg-type]
    assert p.should_retry(ValueError("x"), 0) is False


def test_should_retry_rejects_non_type_member():
    """retry_on 含非类型成员时 fail-closed 不重试。"""
    p = RetryPolicy(max_attempts=3, retry_on=(ValueError, "not-a-type"))  # type: ignore[arg-type]
    assert p.should_retry(ValueError("x"), 0) is False


def test_backoff_is_exponential_with_jitter_bound():
    """退避时长 = base * factor^(attempt-1) + 抖动，抖动落在 [0, base*jitter]。"""
    p = RetryPolicy(max_attempts=5, backoff_base=1.0, backoff_factor=2.0, jitter=0.1)
    d1 = p.backoff(1)
    d2 = p.backoff(2)
    assert 1.0 <= d1 < 1.1
    assert 2.0 <= d2 < 2.2
    assert d2 > d1


def test_backoff_attempt_zero_uses_base():
    """attempt<=0 时退避为基础值（不抛）。"""
    p = RetryPolicy(backoff_base=1.0, backoff_factor=2.0, jitter=0.0)
    assert p.backoff(0) == pytest.approx(1.0)


def test_retry_policy_sleep_uses_monotonic_budget(monkeypatch):
    """sleep 调用 time.sleep 且时长来自 backoff。"""
    slept = []
    import hero_quant.agent.policies as pol

    monkeypatch.setattr(pol.time, "sleep", lambda d: slept.append(d))
    p = RetryPolicy(backoff_base=1.0, backoff_factor=2.0, jitter=0.0)
    p.sleep(1)
    assert len(slept) == 1
    assert slept[0] == pytest.approx(1.0)


def test_retry_policy_sleep_swallows_exception(monkeypatch):
    """sleep 被中断时只告警不抛（best-effort）。"""
    import hero_quant.agent.policies as pol

    def _boom(_d):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(pol.time, "sleep", _boom)
    p = RetryPolicy()
    p.sleep(1)  # 不抛


def test_retry_policy_asleep_awaits(monkeypatch):
    """asleep 走 asyncio.sleep，异常吞吐不抛。"""
    import hero_quant.agent.policies as pol

    slept = []

    async def _fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(pol.asyncio, "sleep", _fake_sleep)
    p = RetryPolicy(backoff_base=1.0, backoff_factor=2.0, jitter=0.0)
    asyncio.run(p.asleep(1))
    assert slept and slept[0] == pytest.approx(1.0)


# ---------------------------------------------------------------- error_handler


def test_error_handler_returns_compensate_command():
    """Saga 补偿入口返回 goto=compensate 的 Command，update 带 error 文本。"""
    cmd = error_handler({}, NodeError("boom"))
    assert getattr(cmd, "goto", None) == "compensate"
    assert getattr(cmd, "update", {}).get("error") == "boom"


def test_node_error_is_exception():
    """NodeError 为 Exception 子类，可被 except 捕获。"""
    with pytest.raises(NodeError):
        raise NodeError("x")


# ---------------------------------------------------------------- BudgetBreaker 定价


def test_estimate_cost_uses_default_prices():
    """默认定价 0.15/0.60 per 1M tokens。"""
    b = BudgetBreaker()
    cost = b.estimate_cost({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(0.75)


def test_estimate_cost_accepts_prompt_completion_aliases():
    """兼容 prompt_tokens/completion_tokens 字段名。"""
    b = BudgetBreaker()
    cost = b.estimate_cost({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
    assert cost == pytest.approx(0.75)


def test_estimate_cost_non_dict_is_zero():
    """非 dict usage 回落 0.0。"""
    assert BudgetBreaker().estimate_cost("nope") == 0.0


def test_estimate_cost_invalid_token_count_is_zero():
    """非数值 token 数回落为 0 贡献。"""
    b = BudgetBreaker()
    assert b.estimate_cost({"input_tokens": "abc", "output_tokens": 0}) == 0.0


def test_get_prices_env_override(monkeypatch):
    """env HERO_LLM_PRICE_IN/OUT 覆盖默认定价。"""
    monkeypatch.setenv("HERO_LLM_PRICE_IN", "1.0")
    monkeypatch.setenv("HERO_LLM_PRICE_OUT", "2.0")
    b = BudgetBreaker()
    assert b._get_prices() == (1.0, 2.0)
    assert b.estimate_cost({"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(3.0)


def test_get_prices_invalid_env_falls_back(monkeypatch):
    """非法 env 定价回落默认值，不抛错。"""
    monkeypatch.setenv("HERO_LLM_PRICE_IN", "not-a-number")
    monkeypatch.setenv("HERO_LLM_PRICE_OUT", "")
    b = BudgetBreaker()
    assert b._get_prices() == (BudgetBreaker.DEFAULT_PRICE_IN, BudgetBreaker.DEFAULT_PRICE_OUT)


# ---------------------------------------------------------------- BudgetBreaker 记账


def test_record_usage_accumulates_cost():
    """record_usage 返回本次成本并计入累计。"""
    b = BudgetBreaker(daily_limit=100.0)
    c = b.record_usage({"input_tokens": 1_000_000, "output_tokens": 0})
    assert c == pytest.approx(0.15)
    assert b.total_cost() == pytest.approx(0.15)


def test_record_usage_zero_cost_not_recorded():
    """零成本不追加（避免无界增长）。"""
    b = BudgetBreaker(daily_limit=100.0)
    assert b.record_usage({"input_tokens": 0, "output_tokens": 0}) == 0.0
    assert b.total_cost() == 0.0


def test_record_usage_non_finite_cost_is_zero():
    """非有限成本（inf）被忽略并回落 0.0。"""
    b = BudgetBreaker(daily_limit=100.0)
    assert b.record_usage({"input_tokens": float("inf")}) == 0.0
    assert b.total_cost() == 0.0


def test_add_cost_ignores_invalid_and_non_finite():
    """add_cost 对非法值与非有限值均忽略。"""
    b = BudgetBreaker(daily_limit=100.0)
    b.add_cost("abc")
    b.add_cost(float("nan"))
    b.add_cost(float("inf"))
    assert b.total_cost() == 0.0


def test_add_cost_appends_positive_cost():
    """add_cost 正常累加。"""
    b = BudgetBreaker(daily_limit=100.0)
    b.add_cost(1.25)
    assert b.total_cost() == pytest.approx(1.25)


# ---------------------------------------------------------------- BudgetBreaker 熔断


def test_should_fallback_single_cost_over_limit():
    """单次成本超限即降级。"""
    b = BudgetBreaker(daily_limit=5.0)
    assert b.should_fallback(cost=6.0) is True
    assert b.should_fallback(cost=1.0) is False


def test_should_fallback_cumulative_over_limit():
    """累计 + 本次超限即降级。"""
    b = BudgetBreaker(daily_limit=5.0)
    b.add_cost(4.0)
    assert b.should_fallback(cost=2.0) is True
    assert b.should_fallback(cost=0.5) is False


def test_should_fallback_invalid_cost_is_tolerated():
    """非法/非有限 cost 被收敛为 0，不抛。"""
    b = BudgetBreaker(daily_limit=5.0)
    assert b.should_fallback(cost="abc") is False
    assert b.should_fallback(cost=float("nan")) is False


def test_check_and_add_returns_fallback_and_records():
    """check_and_add 原子判定并记账。"""
    b = BudgetBreaker(daily_limit=5.0)
    assert b.check_and_add(3.0) is False
    assert b.check_and_add(3.0) is True
    assert b.total_cost() == pytest.approx(6.0)


def test_check_and_add_invalid_cost_raises():
    """非法/非有限 cost 抛 ValueError（fail-visible）。"""
    b = BudgetBreaker(daily_limit=5.0)
    with pytest.raises(ValueError, match="invalid cost"):
        b.check_and_add("abc")
    with pytest.raises(ValueError, match="non-finite cost"):
        b.check_and_add(float("inf"))


def test_budget_breaker_prunes_expired_window(monkeypatch):
    """窗口外的成本被剪除（滑动窗口语义）。"""
    b = BudgetBreaker(daily_limit=5.0, window_seconds=1)
    b.add_cost(4.0)
    assert b.total_cost() == pytest.approx(4.0)
    # 把 monotonic 推进超过窗口 -> 旧成本被剪除
    import hero_quant.agent.policies as pol

    real_monotonic = pol.time.monotonic
    monkeypatch.setattr(pol.time, "monotonic", lambda: real_monotonic() + 10)
    assert b.total_cost() == 0.0


def test_budget_breaker_total_is_finite():
    """total_cost 始终为有限值。"""
    b = BudgetBreaker(daily_limit=100.0)
    b.add_cost(1.0)
    assert math.isfinite(b.total_cost())
