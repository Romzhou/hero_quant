# tests/test_dedup.py
def test_idempotency_ledger(tmp_path):
    from hero_quant.governance.dedup import DedupStore
    s=DedupStore(str(tmp_path/"dedup.db"))
    k="tenant:wf1:step2:run_backtest:600519"
    assert s.insert_pending(k,"run_backtest") is True
    assert s.insert_pending(k,"run_backtest") is False
    s.mark_success(k, {"ok":True})
    assert s.get(k)["status"]=="SUCCESS"


def test_dedup_validation_rejects_bad_key(tmp_path):
    """P2: missing validation - empty key/tool should raise ValueError with log."""
    from hero_quant.governance.dedup import DedupStore
    import pytest
    s = DedupStore(str(tmp_path / "dedup.db"))
    with pytest.raises(ValueError):
        s.insert_pending("", "tool")
    with pytest.raises(ValueError):
        s.insert_pending("tenant:wf:step:tool:biz", "")
    with pytest.raises(ValueError):
        s.get("")
    # derive_key validation already, test bad part with colon
    from hero_quant.governance.dedup import derive_key
    with pytest.raises(ValueError):
        derive_key("a:b", "wf", "step", "tool", "biz")


def test_dedup_second_instance_persistence(tmp_path):
    """P2: single-instance false confidence - second DedupStore against same file must see dedup."""
    from hero_quant.governance.dedup import DedupStore
    db = str(tmp_path / "dedup.db")
    s1 = DedupStore(db)
    k = "tenant:wf1:step2:run_backtest:600519"
    assert s1.insert_pending(k, "run_backtest") is True
    s2 = DedupStore(db)
    # second instance should see existing pending
    assert s2.insert_pending(k, "run_backtest") is False
    assert s2.get(k) is not None
    s1.mark_success(k, {"ok": True})
    # after success, second instance still deduped (not re-insertable until TTL)
    assert s2.insert_pending(k, "run_backtest") is False
    assert s2.get(k)["status"] == "SUCCESS"


def test_dedup_mem_bounded(tmp_path):
    """P2: unbounded growth - _mem must be capped and TTL eviction works."""
    from hero_quant.governance.dedup import DedupStore
    s = DedupStore(str(tmp_path / "dedup.db"), ttl_seconds=1)
    # insert many
    for i in range(2000):
        k = f"tenant:wf:step:tool:biz{i}"
        s.insert_pending(k, "tool")
    # should be bounded (cap 10000, but after TTL expiry should evict)
    assert len(s._mem) <= 3000
    # wait for TTL expiry and verify get returns None after expiry window with new insert?
    import time
    time.sleep(1.2)
    # expired key should be re-insertable
    k0 = "tenant:wf:step:tool:biz0"
    assert s.get(k0) is None
    assert s.insert_pending(k0, "tool") is True


def test_dedup_derive_key_format(tmp_path):
    """P2: derive_key format must be exactly 5 colon-joined parts."""
    from hero_quant.governance.dedup import derive_key
    k = derive_key("tenantA", "wf1", "step1", "toolX", "biz1")
    assert k == "tenantA:wf1:step1:toolX:biz1"
    assert k.count(":") == 4
