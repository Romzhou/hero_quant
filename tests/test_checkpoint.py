def test_checkpoint_roundtrip():
    from hero_quant.checkpoint.postgres import get_saver
    saver=get_saver(dsn="memory://test")
    tid="backtest:1:tenantA"
    saver.put(tid, {"step":1}, {"next":"plan"})
    assert saver.get(tid)["step"]==1
