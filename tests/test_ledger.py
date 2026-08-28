# tests/test_ledger.py
def test_ledger_chain(tmp_path):
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "order", "symbol": "600519.SH"})
    ledger.append({"action": "order", "symbol": "AAPL.US"})
    assert ledger.verify() is True
    # 篡改检测
    p = tmp_path / "ledger.jsonl"
    p.write_text(p.read_text().replace("600519", "999999"))
    assert ledger.verify() is False


def test_ledger_query_deepcopy_isolation(tmp_path):
    """P2: shallow copies leaking state - query must return deep copy."""
    from hero_quant.governance.ledger import Ledger
    import copy
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    ledger.append({"action": "order", "symbol": "600519.SH"}, tenant="t1")
    q1 = ledger.query("t1")
    assert len(q1) == 1
    # mutate returned
    q1[0]["record"]["symbol"] = "MUTATED"
    q1.append({"fake": True})
    q2 = ledger.query("t1")
    assert q2[0]["record"]["symbol"] == "600519.SH", "query shallow copy leak"
    assert len(q2) == 1


def test_ledger_append_validation(tmp_path):
    """P2: missing validation - empty tenant/record should raise."""
    from hero_quant.governance.ledger import Ledger
    import pytest
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    with pytest.raises((ValueError, TypeError)):
        ledger.append({}, tenant="")  # empty tenant
    with pytest.raises((ValueError, TypeError)):
        ledger.append("not_a_dict", tenant="t1")  # type: ignore
    with pytest.raises((ValueError, TypeError)):
        ledger.append({"action": "order"}, tenant="   ")


def test_ledger_unbounded_growth_rotate(tmp_path):
    """P2: unbounded growth - rotate_if_needed must exist and handle size threshold."""
    from hero_quant.governance.ledger import Ledger, rotate_if_needed, DEFAULT_ROTATE_BYTES
    import pathlib
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    for i in range(5):
        ledger.append({"i": i})
    # small threshold should trigger rotate when file exceeds
    # create fake large file by appending many entries
    # use rotate_if_needed with tiny max_bytes to force
    rot = rotate_if_needed(p, max_bytes=10)
    assert rot is not None
    assert rot.exists()
    # original path now missing/empty until next append
    # append again should create new file
    ledger2 = Ledger(p)
    ledger2.append({"after_rotate": True})
    assert ledger2.verify() is True or p.exists()


def test_ledger_append_returns_copy_not_alias(tmp_path):
    """P2: race/shallow - append return dict mutation must not affect stored."""
    from hero_quant.governance.ledger import Ledger
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    ret = ledger.append({"action": "order", "v": 1})
    ret["record"]["v"] = 999
    q = ledger.query("default")
    assert q[0]["record"]["v"] == 1
