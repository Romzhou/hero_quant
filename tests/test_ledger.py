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
