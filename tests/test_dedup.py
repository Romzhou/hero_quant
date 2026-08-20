# tests/test_dedup.py
def test_idempotency_ledger(tmp_path):
    from hero_quant.governance.dedup import DedupStore
    s=DedupStore(str(tmp_path/"dedup.db"))
    k="tenant:wf1:step2:run_backtest:600519"
    assert s.insert_pending(k,"run_backtest") is True
    assert s.insert_pending(k,"run_backtest") is False
    s.mark_success(k, {"ok":True})
    assert s.get(k)["status"]=="SUCCESS"
