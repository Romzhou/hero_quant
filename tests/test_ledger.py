# tests/test_ledger.py
def test_ledger_chain(tmp_path):
    from hero_quant.governance.ledger import Ledger
    l = Ledger(tmp_path / "ledger.jsonl")
    l.append({"action":"order","symbol":"600519.SH"})
    l.append({"action":"order","symbol":"AAPL.US"})
    assert l.verify() is True
    # 篡改检测
    p = tmp_path / "ledger.jsonl"
    p.write_text(p.read_text().replace("600519","999999"))
    assert l.verify() is False
