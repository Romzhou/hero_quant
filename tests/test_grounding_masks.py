"""W1-A A3 grounding masks 8 classes."""
import pytest


def test_extract_claims_basic():
    from hero_quant.agent.grounding import extract_claims
    text = "价格1,234.56，涨幅10%，日期2026-08-19，数量300手"
    claims = extract_claims(text)
    # helper to find by type
    def find(t):
        return [c for c in claims if c.get("type") == t or c.get("kind") == t]
    # price with thousand normalization
    price_claims = [c for c in claims if c.get("type") in ("price", "thousand", "currency", "negative") or "price" in c or "value" in c]
    # at least one price ~1234.56
    assert any(abs(float(c.get("value", c.get("price", 0))) - 1234.56) < 0.001 for c in claims if isinstance(c.get("value", c.get("price")), (int, float)))
    # percent 10.0
    assert any(abs(float(c.get("value", 0)) - 10.0) < 0.001 for c in claims if c.get("type") in ("percent", "pct", "percentage") or (c.get("raw","").endswith("%") or "%" in str(c.get("raw",""))))
    # date
    assert any(c.get("value") == "2026-08-19" or c.get("raw") == "2026-08-19" or "2026-08-19" in str(c.values()) for c in claims)
    # quantity 300
    assert any(int(c.get("value", 0)) == 300 for c in claims if c.get("type") in ("quantity", "qty") or "手" in str(c.get("raw","")))


def test_assert_price_normalizes_thousand():
    from hero_quant.agent.grounding import GroundingLedger
    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": 1500.0, "low": 1400.0, "high": 1600.0, "date": "2026-08-19"}])
    # string "1,500" should normalize to 1500
    ledger.assert_price("600519.SH", "1,500")
    # also with currency
    ledger.assert_price("600519.SH", "$1,500")
    # via extract integration, price extracted as string with comma should still pass


def test_eight_mask_types():
    from hero_quant.agent.grounding import extract_claims
    # price
    c1 = extract_claims("价格123.45")
    assert any("123.45" in str(v) or abs(float(c.get("value",0))-123.45)<0.001 for c in c1 for v in [c.get("value"), c.get("raw")] if v is not None)
    # thousand
    c2 = extract_claims("1,234.56")
    assert any(abs(float(c.get("value",0))-1234.56)<0.001 for c in c2)
    # percent
    c3 = extract_claims("涨幅10%")
    assert any("percent" in c.get("type","") or "%" in str(c.get("raw","")) for c in c3)
    # date
    c4 = extract_claims("日期2026-08-19")
    assert any("2026-08-19" in str(c.values()) for c in c4)
    # quantity
    c5 = extract_claims("数量300手")
    assert any("quantity" in c.get("type","") or "qty" in c.get("type","") or "300" in str(c.get("raw","")) for c in c5)
    # range / interval
    c6 = extract_claims("区间100-200")
    assert any("range" in c.get("type","") or "interval" in c.get("type","") or (isinstance(c.get("value"), list) and len(c.get("value"))==2) for c in c6)
    # currency
    c7 = extract_claims("价格$1,234.56")
    assert any("currency" in c.get("type","") or "$" in str(c.get("raw","")) for c in c7)
    # negative
    c8 = extract_claims("价格-10.5")
    assert any(c.get("value") == -10.5 or (isinstance(c.get("value"), (int,float)) and float(c.get("value")) <0) or c.get("type")=="negative" or "-10.5" in str(c.get("raw","")) for c in c8)


def test_loop_claim_extraction_uses_extract_claims():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 1234.56, "low": 1200.0, "high": 1300.0, "date": "2026-08-19"}])

    class FakeLLM:
        def stream_chat(self, goal):
            # text contains price with thousand separator
            yield {"type": "text", "text": "AAPL 价格1,234.56 在 2026-08-19"}

    loop = AgentLoop(llm=FakeLLM(), max_iterations=2, grounding=ledger)
    result = loop.run("test claim loop")
    # Should be grounding_verified true because price normalized
    assert result.grounding_verified is True
    assert result.terminated is True


def test_loop_grounding_ignores_percent_quantity_date_range():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 150.0, "low": 140.0, "high": 160.0}])

    class FakeLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "涨幅10% 数量300手 日期2026-08-19 区间100-200"}

    loop = AgentLoop(llm=FakeLLM(), max_iterations=1, grounding=ledger)
    result = loop.run("percent quantity date range")
    assert result.grounding_verified is True
    assert result.reason == "completed"


def test_graph_path_uses_same_extract_claims_rule():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}])

    class FakeGraph:
        def invoke(self, state):
            return {"messages": [{"role": "assistant", "content": "涨幅10% 数量300手 区间100-200 日期2026-08-19"}]}

    # Dummy LLM not used in graph mode but required for init
    class DummyLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "unused"}

    loop = AgentLoop(llm=DummyLLM(), max_iterations=2, grounding=ledger, use_graph=True, graph=FakeGraph())
    result = loop._run_graph("graph percent only")
    # graph path must use same extract_claims filtering, thus no price claim => verified True
    assert result.grounding_verified is True
    assert result.reason == "completed"


def test_graph_path_percent_only_not_false_negative():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.grounding import GroundingLedger
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 1234.56, "low": 1200.0, "high": 1300.0}])

    class FakeGraph2:
        def invoke(self, state):
            # only percent, should not be treated as price
            return {"messages": [{"role": "assistant", "content": "今日涨幅10% 数量300股"}]}

    class DummyLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "unused"}

    loop = AgentLoop(llm=DummyLLM(), grounding=ledger, use_graph=True, graph=FakeGraph2())
    result = loop._run_graph("graph percent 2")
    assert result.grounding_verified is True
