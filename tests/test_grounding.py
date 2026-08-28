def test_grounding_blocks_hallucinated_price():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": 1500.0, "date":"2026-08-19"}])
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
    import pytest
    ledger = GroundingLedger()
    # missing evidence should fail closed
    with pytest.raises(GroundingError):
        ledger.assert_price("UNKNOWN", 100.0)
    # ingest with edge close values
    ledger.ingest("AAPL", [{"close": 100.0}])
    # non-numeric price should raise not silently pass
    with pytest.raises((GroundingError, ValueError, TypeError)):
        ledger.assert_price("AAPL", "not-a-number")  # type: ignore
    # numeric string should be coerced
    ledger.assert_price("AAPL", "100.0")  # type: ignore should not raise if coerced, but if strict it raises; ensure not silent
    # very large deviation should block
    with pytest.raises(GroundingError):
        ledger.assert_price("AAPL", 200.0)
