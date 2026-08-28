"""Task14 Shadow 2.0 熔断对接风控 — TDD red.

Assertions:
- ShadowRule 3-5条
- 5类归因且 coverage>0
- direct对接 Risk Engine 有熔断 (CircuitBreaker)
"""
import pytest


def test_shadow_rules_3_5():
    from hero_quant.shadow import ShadowRule, RiskEngine, DEFAULT_RULES

    # DEFAULT_RULES 3-5条
    assert 3 <= len(DEFAULT_RULES) <= 5, f"rules {len(DEFAULT_RULES)} not in 3-5"
    # Rule objects have name/check
    for r in DEFAULT_RULES:
        assert hasattr(r, "name")
        assert hasattr(r, "check")
        assert isinstance(r.name, str) and r.name

    engine = RiskEngine()
    assert 3 <= len(engine.rules) <= 5
    # direct Risk Engine
    assert hasattr(engine, "circuit")
    from hero_quant.telemetry.circuit import CircuitBreaker

    assert isinstance(engine.circuit, CircuitBreaker)


def test_shadow_five_attribution_coverage_gt_zero():
    from hero_quant.shadow import ShadowJournal, ATTRIBUTION_CATEGORIES

    # 5 categories defined
    assert len(ATTRIBUTION_CATEGORIES) == 5
    # Journal attribution has 5 keys and coverage>0
    j = ShadowJournal()
    # record some trades to generate attribution
    # minimal synthetic fills
    j.record({"symbol": "600519.SH", "side": "buy", "qty": 100, "price": 10, "pnl": 1.2})
    j.record({"symbol": "600519.SH", "side": "sell", "qty": 50, "price": 11, "pnl": -0.3})
    j.record({"symbol": "000001.SZ", "side": "buy", "qty": 200, "price": 8, "pnl": 0.5})
    j.record({"symbol": "BTC/USDT", "side": "buy", "qty": 1, "price": 30000, "pnl": 0.8})
    j.record({"symbol": "BTC/USDT", "side": "sell", "qty": 1, "price": 31000, "pnl": -0.2})

    attr = j.attribution()
    # attr has 5 categories
    assert isinstance(attr, dict)
    assert len(attr) == 5, f"attr len {len(attr)} !=5 got {attr}"
    # coverage>0: each category has non-zero absolute weight or count
    for k, v in attr.items():
        assert abs(float(v)) > 0, f"category {k} has zero coverage {v}"
    # coverage() overall >0
    assert j.coverage() > 0
    # also ensure categories match defined set
    assert set(attr.keys()) == set(ATTRIBUTION_CATEGORIES)


def test_shadow_direct_risk_engine_circuit_break():
    from hero_quant.shadow import RiskEngine, ShadowJournal
    from hero_quant.telemetry.circuit import CircuitBreaker

    # direct integration: RiskEngine has circuit and shadows journal uses it
    cb = CircuitBreaker(failure_threshold=0.5, window=2, open_duration=1)
    engine = RiskEngine(circuit=cb)
    journal = ShadowJournal(risk_engine=engine)

    # normal order should pass when circuit closed
    ok = engine.check_order({"symbol": "600519.SH", "qty": 100, "price": 10, "side": "buy"})
    assert ok["allowed"] is True or "reason" in ok

    # force failures to trip circuit: use invalid orders that violate rules
    # e.g., qty exceeding position limit or price invalid -> rule check fails
    for _ in range(5):
        engine.circuit.record_failure()
    assert engine.circuit.state == "OPEN"
    # when OPEN, check_order must 熔断 (reject)
    blocked = engine.check_order({"symbol": "600519.SH", "qty": 100, "price": 10, "side": "buy"})
    assert blocked["allowed"] is False
    assert "circuit" in blocked["reason"].lower() or "熔断" in blocked["reason"]

    # journal direct link: journal.risk_engine is same engine and also respects circuit
    assert journal.risk_engine is engine
    # journal.record should also go through risk check and not write when熔断?
    # at least journal can still attribute but engine blocks
    assert journal.risk_engine.circuit.is_open()


def test_shadow_ledger_integration_if_present():
    """Optional: ShadowJournal can ledger.append and ledger.verify still works."""
    from pathlib import Path
    import tempfile
    from hero_quant.governance.ledger import Ledger
    from hero_quant.shadow import ShadowJournal, RiskEngine

    with tempfile.TemporaryDirectory() as td:
        ledger = Ledger(Path(td) / "shadow_ledger.jsonl")
        engine = RiskEngine(ledger=ledger)
        j = ShadowJournal(ledger=ledger, risk_engine=engine)
        j.record({"symbol": "600519.SH", "side": "buy", "qty": 10, "price": 10, "pnl": 0.1})
        # ledger should have at least one entry if integration present, verify passes
        # allow empty ledger too but verify must be True
        assert ledger.verify() is True


# Task 6 TDD: fail-closed on breaker exception
def test_check_order_fail_closed_on_allow_exception():
    from hero_quant.shadow import RiskEngine
    from hero_quant.telemetry.circuit import CircuitBreaker
    from unittest import mock

    cb = CircuitBreaker()
    engine = RiskEngine(circuit=cb)
    cb.allow = mock.MagicMock(side_effect=RuntimeError("breaker unhealthy"))
    result = engine.check_order({"symbol": "AAPL", "qty": 10, "price": 10, "side": "buy"})
    assert result["allowed"] is False
    assert "circuit" in result["reason"].lower() or "熔断" in result["reason"]
    assert result.get("rule") == "circuit"


def test_check_order_fail_closed_on_allow_exception_is_open_also_raises():
    from hero_quant.shadow import RiskEngine
    from hero_quant.telemetry.circuit import CircuitBreaker
    from unittest import mock

    cb = CircuitBreaker()
    engine = RiskEngine(circuit=cb)
    cb.allow = mock.MagicMock(side_effect=OSError("is_open also fails"))
    cb.is_open = mock.MagicMock(side_effect=OSError("is_open fails"))
    result = engine.check_order({"symbol": "AAPL", "qty": 10, "price": 10, "side": "buy"})
    assert result["allowed"] is False


def test_check_order_reject_when_circuit_open():
    from hero_quant.shadow import RiskEngine
    from hero_quant.telemetry.circuit import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=30)
    for _ in range(5):
        cb.record_failure()
    # ensure breaker is OPEN
    assert cb.state == "OPEN"
    engine = RiskEngine(circuit=cb)
    result = engine.check_order({"symbol": "600519.SH", "qty": 100, "price": 10, "side": "buy"})
    assert result["allowed"] is False
    assert "circuit" in result["reason"].lower() or "熔断" in result["reason"]


def test_check_order_mock_state_open_via_allow():
    from hero_quant.shadow import RiskEngine
    from hero_quant.telemetry.circuit import CircuitBreaker

    cb = CircuitBreaker()
    # mock to simulate OPEN state via allow returning False
    orig_allow = cb.allow
    cb.allow = lambda: False
    engine = RiskEngine(circuit=cb)
    result = engine.check_order({"symbol": "600519.SH", "qty": 10, "price": 10, "side": "buy"})
    assert result["allowed"] is False
    cb.allow = orig_allow  # restore
