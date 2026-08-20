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
