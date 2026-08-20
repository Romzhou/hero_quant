def test_sandbox_escape_blocked():
    from hero_quant.sandbox.ast_guard import check_import_allowlist

    assert check_import_allowlist('import os; os.system("rm -rf /")') is False
    assert check_import_allowlist('eval("open(__import__(\"os\").path)")') is False


def test_redaction_no_leak():
    from hero_quant.security.redaction import redact_payload

    payload = {"api_key": "sk-1234567890abcdef", "content": "hello"}
    r = redact_payload(payload, sink="arguments")
    assert "sk-123" not in str(r) and "***" in str(r)
    r2 = redact_payload(payload, sink="result")
    assert r2["content"] == "hello"


def test_ledger_tamper_detected(tmp_path):
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "order", "symbol": "600519.SH"})
    ledger.append({"action": "order", "symbol": "AAPL.US"})
    assert ledger.verify() is True
    p = tmp_path / "ledger.jsonl"
    p.write_text(p.read_text().replace("600519", "999999"))
    assert ledger.verify() is False


def test_path_traversal_blocked(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    p = TraceWriter._safe_sidecar_path(tmp_path, "../escape.txt")
    assert p is None


def test_approval_never_blocks():
    from hero_quant.security.approval import ApprovalService

    svc = ApprovalService(mode="never")
    assert svc.request_sync(tool="run_backtest", reason="x") == "rejected"
