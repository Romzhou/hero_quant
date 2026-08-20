# tests/test_security_approval.py
def test_approval_never_shortcuts(tmp_path):
    from hero_quant.security.approval import ApprovalService
    svc = ApprovalService(mode="never")
    outcome = svc.request_sync(tool="run_backtest", reason="高风险")
    assert outcome=="rejected"
    from hero_quant.security.redaction import redact_payload
    assert redact_payload({"api_key":"sk-xxx"}, sink="arguments")["api_key"]=="***"
