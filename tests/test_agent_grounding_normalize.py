from hero_quant.agent.grounding import GroundingError, GroundingLedger


def test_ingest_formatted_price():
    g = GroundingLedger()
    g.ingest("600519.SH", [{"close": "1,500", "low": "$1,400", "high": "¥1,600"}])
    g.assert_price("600519.SH", "1,500")


def test_empty_bars_rejects_zero():
    g = GroundingLedger()
    g.ingest("X", [])
    try:
        g.assert_price("X", 0)
        assert False, "should raise GroundingError"
    except GroundingError as e:
        assert "empty evidence" in str(e).lower()


def test_authorized_type_error():
    g = GroundingLedger()
    g.ingest("A", [{"close": 10}])
    try:
        g.assert_price("A", 10, authorized="bad")
        assert False, "should raise TypeError"
    except TypeError:
        pass
