"""T1-6 存量兼容：ingest 现默认要求 provenance，旧调用补 provenance 后行为不变。"""
import pytest

PROV = {"source": "test", "unit": "shares"}


def test_grounding_blocks_hallucinated_price():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": 1500.0, "date": "2026-08-19"}], provenance=PROV)
    # 未在 ledger 中的价格必须被拦
    try:
        ledger.assert_price("600519.SH", 9999.0)
        assert False, "should raise"
    except GroundingError as e:
        assert "not in evidence" in str(e).lower()
    # 在 evidence 范围内的通过
    ledger.assert_price("600519.SH", 1500.0)


def test_grounding_assert_price_unsafe_cast_and_missing_evidence():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    ledger = GroundingLedger()
    # missing evidence should fail closed
    with pytest.raises(GroundingError):
        ledger.assert_price("UNKNOWN", 100.0)
    # ingest with edge close values
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=PROV)
    # non-numeric price should raise not silently pass
    with pytest.raises((GroundingError, ValueError, TypeError)):
        ledger.assert_price("AAPL", "not-a-number")  # type: ignore
    # numeric string should be coerced
    ledger.assert_price("AAPL", "100.0")  # type: ignore should not raise if coerced, but if strict it raises; ensure not silent
    # very large deviation should block
    with pytest.raises(GroundingError):
        ledger.assert_price("AAPL", 200.0)


def test_grounding_ingest_requires_provenance():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    g = GroundingLedger()
    with pytest.raises(GroundingError):
        g.ingest("AAPL", [{"close": 99999.0, "date": "2024-01-01"}])
    with pytest.raises(GroundingError):
        g.ingest("AAPL", [{"close": 100.0}], provenance={"source": "", "unit": "shares"})
    with pytest.raises(GroundingError):
        g.ingest("AAPL", [{"close": 100.0}], provenance={"source": "x", "unit": "hand"})
    # 非数值 close 拒收
    with pytest.raises(GroundingError):
        g.ingest("AAPL", [{"close": "not-a-number"}], provenance=PROV)


def test_grounding_render_uses_normalized_values():
    from hero_quant.agent.grounding import GroundingLedger
    g = GroundingLedger()
    g.ingest("600519.SH", [{"close": "1,500", "low": "$1,400", "high": "¥1,600", "date": "2026-08-19"}],
             provenance=PROV)
    block = g.render_block()
    assert "1,500" not in block and "$1,400" not in block
    assert "1500.0" in block


def test_grounding_frozen_snapshot_requires_authorized_for_new_symbol():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=PROV)
    g.assert_price("AAPL", 100.0)  # 首冻 symbol 放行（旧测试兼容）
    g.ingest("TSLA", [{"close": 200.0}], provenance=PROV)
    with pytest.raises(GroundingError):
        g.assert_price("TSLA", 200.0)  # 非冻结 symbol 需显式授权
    g.assert_price("TSLA", 200.0, authorized=frozenset({"AAPL", "TSLA"}))
