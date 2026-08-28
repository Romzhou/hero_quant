"""W1-A A1/A2 TDD: BudgetBreaker real usage + batch frozen identity."""
import os
import pytest


def test_budget_breaker_record_usage_default_pricing():
    from hero_quant.agent.policies import BudgetBreaker
    # ensure env clean
    os.environ.pop("HERO_LLM_PRICE_IN", None)
    os.environ.pop("HERO_LLM_PRICE_OUT", None)
    bb = BudgetBreaker(daily_limit=5.0)
    cost = bb.record_usage({"input_tokens": 100_000, "output_tokens": 50_000})
    # default $0.15 / $0.60 per 1M
    assert cost == pytest.approx(100_000 * 0.15 / 1_000_000 + 50_000 * 0.60 / 1_000_000, rel=1e-6)
    # also total_cost should reflect
    assert bb.total_cost() == pytest.approx(cost, rel=1e-6)
    # not fallback yet
    assert bb.should_fallback(cost=0) is False


def test_budget_breaker_env_override():
    from hero_quant.agent.policies import BudgetBreaker
    os.environ["HERO_LLM_PRICE_IN"] = "1.0"
    os.environ["HERO_LLM_PRICE_OUT"] = "2.0"
    try:
        bb = BudgetBreaker(daily_limit=5.0)
        cost = bb.record_usage({"input_tokens": 100_000, "output_tokens": 50_000})
        assert cost == pytest.approx(100_000 * 1.0 / 1_000_000 + 50_000 * 2.0 / 1_000_000, rel=1e-6)
    finally:
        os.environ.pop("HERO_LLM_PRICE_IN", None)
        os.environ.pop("HERO_LLM_PRICE_OUT", None)


def test_budget_breaker_cumulative_exceeds_daily_limit():
    from hero_quant.agent.policies import BudgetBreaker
    os.environ.pop("HERO_LLM_PRICE_IN", None)
    os.environ.pop("HERO_LLM_PRICE_OUT", None)
    bb = BudgetBreaker(daily_limit=0.05)
    c1 = bb.record_usage({"input_tokens": 100_000, "output_tokens": 50_000})
    assert c1 == pytest.approx(0.045, rel=1e-6)
    # still under limit
    assert bb.should_fallback(cost=0) is False
    c2 = bb.record_usage({"input_tokens": 100_000, "output_tokens": 50_000})
    # cumulative 0.09 >0.05
    assert bb.total_cost() == pytest.approx(0.09, rel=1e-6)
    assert bb.should_fallback(cost=0) is True
    # also direct cost param
    assert bb.should_fallback(cost=0.01) is True


def test_budget_breaker_fallback_no_usage_old_formula():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.policies import BudgetBreaker
    # loop with no usage should fallback via old formula token_count/10000+iterations*0.05
    # Use daily_limit very low to trigger fallback via old formula
    bb = BudgetBreaker(daily_limit=0.05)

    class FakeLLM:
        def stream_chat(self, goal):
            # no usage_metadata
            yield {"type": "text", "text": "hello " * 5000}  # large buffer to inflate token_count

    loop = AgentLoop(llm=FakeLLM(), max_iterations=1, token_limit=100000, budget_breaker=bb)
    result = loop.run("test fallback no usage")
    # With old formula: token_count ~ len("hello "*5000)/4 ~ 7500/4? actually "hello "*5000 = 30000 chars => 7500 tokens => 0.75 +0.05=0.80 >0.05 => should fallback
    assert result.reason == "budget_fallback"


def test_loop_collects_usage_and_budget_fallback():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.policies import BudgetBreaker
    os.environ.pop("HERO_LLM_PRICE_IN", None)
    os.environ.pop("HERO_LLM_PRICE_OUT", None)
    bb = BudgetBreaker(daily_limit=0.04)  # lower than 0.045 default cost
    # Fake LLM yields usage_metadata with 100k/50k => cost 0.045 >0.04 => should fallback

    class FakeLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "done", "usage_metadata": {"input_tokens": 100_000, "output_tokens": 50_000}}

    loop = AgentLoop(llm=FakeLLM(), max_iterations=2, budget_breaker=bb)
    result = loop.run("test usage fallback")
    assert result.reason == "budget_fallback"


# A2 batch frozen identity
def test_batch_frozen_identity():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger
    from hero_quant.tools.registry import TOOL_REGISTRY

    # cleanup any prior dummy tools
    for n in ["get_market_data", "assert_price"]:
        TOOL_REGISTRY.pop(n, None)

    ledger = GroundingLedger()

    # define tools
    from hero_quant.tools.registry import tool

    @tool(name="get_market_data", description="ingest", is_concurrency_safe=True, parameters={"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}, output={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]})
    def get_market_data(symbol: str):
        ledger.ingest(symbol, [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}])
        return {"ok": True, "symbol": symbol}

    @tool(name="assert_price", description="assert", is_concurrency_safe=False, parameters={"type":"object","properties":{"symbol":{"type":"string"},"price":{"type":"number"}},"required":["symbol","price"]}, output={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]})
    def assert_price(symbol: str, price: float, authorized=None):
        # authorized passed by loop snapshot
        ledger.assert_price(symbol, price, authorized=authorized)
        return {"ok": True}

    # Scenario 1: no prior evidence, batch contains ingest then assert with invalid price 9999 -> second must be rejected via frozen snapshot
    class FakeLLM1:
        def stream_chat(self, goal):
            yield {"tool_calls": [{"name": "get_market_data", "arguments": {"symbol": "TSLA"}}, {"name": "assert_price", "arguments": {"symbol": "TSLA", "price": 9999}}]}

    loop1 = AgentLoop(llm=FakeLLM1(), max_iterations=2, grounding=ledger)
    result1 = loop1.run("batch frozen 1")
    # tool result buffer should contain tool_error for assert_price due to frozen identity
    assert "tool_error" in result1.text or "not in evidence" in result1.text.lower() or "frozen" in result1.text.lower()
    # after batch, ledger does have TSLA because first tool ingested (even though second rejected via snapshot)
    assert "TSLA" in ledger._evidence

    # Scenario 2: prior ingest then batch should pass (authorized snapshot contains TSLA, and price valid)
    ledger2 = GroundingLedger()
    ledger2.ingest("TSLA", [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}])
    # rewire tools to use ledger2
    TOOL_REGISTRY.pop("get_market_data", None)
    TOOL_REGISTRY.pop("assert_price", None)

    @tool(name="get_market_data", description="ingest", is_concurrency_safe=True, parameters={"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}, output={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]})
    def get_market_data2(symbol: str):
        ledger2.ingest(symbol, [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}])
        return {"ok": True, "symbol": symbol}

    @tool(name="assert_price", description="assert", is_concurrency_safe=False, parameters={"type":"object","properties":{"symbol":{"type":"string"},"price":{"type":"number"}},"required":["symbol","price"]}, output={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]})
    def assert_price2(symbol: str, price: float, authorized=None):
        ledger2.assert_price(symbol, price, authorized=authorized)
        return {"ok": True}

    class FakeLLM2:
        def stream_chat(self, goal):
            yield {"tool_calls": [{"name": "get_market_data", "arguments": {"symbol": "TSLA"}}, {"name": "assert_price", "arguments": {"symbol": "TSLA", "price": 150.0}}]}

    loop2 = AgentLoop(llm=FakeLLM2(), max_iterations=2, grounding=ledger2)
    result2 = loop2.run("batch frozen 2")
    # second scenario with prior evidence and valid price should succeed (no tool_error from frozen)
    # The buffer should contain at least one successful assert (no frozen error). If implementation incorrectly rejects, it would contain error.
    # We check that result does NOT contain frozen/not in evidence error for the second call's valid price
    # Since both tools succeed, text should contain ok and not tool_error for authorized check
    # We allow tool_error only if due to price mismatch, but here price 150 is valid, so no error.
    assert "tool_error" not in result2.text.lower() or result2.text.count("tool_error") == 0
    # Ensure result completed
    assert result2.terminated is True

    # cleanup - restore original tools if they were popped
    for n in ["get_market_data", "assert_price"]:
        TOOL_REGISTRY.pop(n, None)
    # restore market_data tools for subsequent tests (e.g., test_tools_entities_registered)
    try:
        import importlib, hero_quant.tools.market_data as _md
        # clear any remaining dummy registrations that would clash on reload
        for _k in list(TOOL_REGISTRY.keys()):
            if _k in ("list_markets", "get_ticker_info", "get_fundamentals", "search_symbols", "search_symbol", "get_bars_range", "search_markets"):
                TOOL_REGISTRY.pop(_k, None)
        importlib.reload(_md)
    except Exception:
        pass


# --- W1-A blocking regression: grounding should ignore percent/quantity/date/range ---
def test_grounding_percent_only_does_not_trigger_failure():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    for n in ["dummy_ok"]:
        TOOL_REGISTRY.pop(n, None)
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}])

    @tool(name="dummy_ok", description="ok", is_concurrency_safe=True, parameters={"type": "object", "properties": {}}, output={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    def dummy_ok():
        return {"ok": True}

    class FakeLLM:
        def __init__(self):
            self.calls = 0
        def stream_chat(self, goal):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "text", "text": "涨幅10% 数量300手 日期2026-08-19 区间100-200"}
                yield {"tool_calls": [{"name": "dummy_ok", "arguments": {}}]}
            else:
                yield {"type": "text", "text": "涨幅5% 数量100股"}

    loop = AgentLoop(llm=FakeLLM(), max_iterations=2, grounding=ledger)
    result = loop.run("percent-only")
    # percent/quantity/date/range must NOT be treated as price claims
    assert result.grounding_verified is True
    assert result.reason != "grounding_failed"
    TOOL_REGISTRY.pop("dummy_ok", None)


def test_grounding_quantity_only_pure_text_verified():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger

    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 150.0, "low": 140.0, "high": 160.0}])

    class FakeLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "数量300手 区间100-200 日期2026/08/19"}

    loop = AgentLoop(llm=FakeLLM(), max_iterations=2, grounding=ledger)
    result = loop.run("quantity-only pure text")
    assert result.grounding_verified is True
    assert result.reason == "completed"


def test_grounding_percent_pure_text_no_false_positive():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger

    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": 1500.0, "low": 1400.0, "high": 1600.0}])

    class FakeLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "今日涨幅10% 明日预估5% 数量300股"}

    loop = AgentLoop(llm=FakeLLM(), max_iterations=1, grounding=ledger)
    result = loop.run("percent pure")
    assert result.grounding_verified is True


# --- W1-A blocking regression: zero-cost must not grow _costs unbounded ---
def test_budget_breaker_zero_cost_does_not_grow():
    from hero_quant.agent.policies import BudgetBreaker
    bb = BudgetBreaker(daily_limit=5.0)
    assert len(bb._costs) == 0
    assert bb.total_cost() == pytest.approx(0.0)
    bb.record_usage({"input_tokens": 0, "output_tokens": 0})
    assert len(bb._costs) == 0
    assert bb.total_cost() == pytest.approx(0.0)
    bb.record_usage({})
    assert len(bb._costs) == 0
    for _ in range(10):
        bb.record_usage({"input_tokens": 0, "output_tokens": 0})
    assert len(bb._costs) == 0
    assert bb.total_cost() == pytest.approx(0.0)


def test_budget_breaker_zero_cost_mixed_with_real_cost():
    from hero_quant.agent.policies import BudgetBreaker
    import os
    os.environ.pop("HERO_LLM_PRICE_IN", None)
    os.environ.pop("HERO_LLM_PRICE_OUT", None)
    bb = BudgetBreaker(daily_limit=5.0)
    c1 = bb.record_usage({"input_tokens": 100_000, "output_tokens": 50_000})
    n1 = len(bb._costs)
    assert n1 == 1
    assert bb.total_cost() == pytest.approx(c1)
    bb.record_usage({"input_tokens": 0, "output_tokens": 0})
    assert len(bb._costs) == n1
    assert bb.total_cost() == pytest.approx(c1)
    bb.record_usage({"input_tokens": 0, "output_tokens": 0})
    assert len(bb._costs) == n1
    # another real cost should still append
    c2 = bb.record_usage({"input_tokens": 100_000, "output_tokens": 50_000})
    assert len(bb._costs) == n1 + 1
    assert bb.total_cost() == pytest.approx(c1 + c2)
