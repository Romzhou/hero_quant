"""B2-1 TDD: trace/ledger auto redaction.
- write_tool_result({"api_key":"sk-123"}) 落盘后为 *** (REDACTED)
- content 透传但顶层 secret 仍脱敏 (ARGUMENTS_SINK vs RESULT_SINK)
"""
import json


def test_trace_tool_result_redacts_but_content_passthrough(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    w = TraceWriter(tmp_path / "trace.jsonl")
    # RESULT_SINK: top-level secret redacted, content stays
    payload = {"type": "tool_result", "tool": "get_bars", "content": "hello world", "api_key": "sk-1234567890abcdef"}
    w.append(payload)
    w.close()
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[0])
    # api_key must be redacted (*** or ***REDACTED***)
    assert rec.get("api_key") in ("***", "***REDACTED***"), rec
    assert "***" in str(rec.get("api_key"))
    assert "sk-123" not in json.dumps(rec)
    # content must pass through unredacted
    assert rec.get("content") == "hello world"
    # also content should be present even if original dict had secret inside content? RESULT_SINK allows content
    # ensure no leak via sidecar path not taken for small content
    assert "sidecar" not in rec and "result_path" not in rec


def test_trace_result_sink_allows_content_with_secret_pattern(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    w = TraceWriter(tmp_path / "trace.jsonl")
    # content contains secret pattern but should NOT be redacted under RESULT_SINK
    secret_in_content = "Bearer eyJ1234567890.abcdef.1234567890"
    payload = {"type": "tool_result", "content": secret_in_content, "api_key": "sk-1234567890abcdef"}
    w.append(payload)
    w.close()
    rec = json.loads((tmp_path / "trace.jsonl").read_text().strip().splitlines()[0])
    assert rec.get("content") == secret_in_content, "RESULT_SINK must allow content passthrough"
    assert rec.get("api_key") in ("***", "***REDACTED***")
    assert "sk-123" not in json.dumps({k: v for k, v in rec.items() if k != "content"})


def test_trace_arguments_sink_redacts_content_secret(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    w = TraceWriter(tmp_path / "trace.jsonl")
    # ARGUMENTS_SINK: non-tool_result should redact content secret pattern too
    payload = {"type": "tool_call", "content": "sk-1234567890abcdef", "api_key": "sk-1234567890abcdef"}
    w.append(payload)
    w.close()
    rec = json.loads((tmp_path / "trace.jsonl").read_text().strip().splitlines()[0])
    # both api_key and content with secret pattern must be redacted
    assert rec.get("api_key") in ("***", "***REDACTED***")
    # content under ARGUMENTS_SINK should be redacted to ***
    assert rec.get("content") in ("***", "***REDACTED***"), rec
    assert "sk-123" not in json.dumps(rec)


def test_trace_large_tool_result_sidecar_still_redacted(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    w = TraceWriter(tmp_path, sidecar_threshold=50)
    payload = {"type": "tool_result", "content": "x" * 100, "api_key": "sk-1234567890abcdef", "secret": "mySecret"}
    w.append(payload)
    w.close()
    # generic sidecar overflow path not taken for tool_result? tool_result uses result_path branch
    # For large content (>50) should still redact api_key while offloading content
    import json
    # This payload content length 100 > threshold 50, so expect result_path
    lines = (tmp_path / "trace.jsonl").read_text().strip().splitlines()
    rec = json.loads(lines[0])
    assert rec.get("api_key") in ("***", "***REDACTED***")
    assert rec.get("secret") in ("***", "***REDACTED***")
    # content was offloaded; preview should equal original content (passthrough)
    assert rec.get("preview") == "x" * 100 or rec.get("content") == "x" * 100 or rec.get("preview") is not None


def test_ledger_auto_redaction(tmp_path):
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    # plain content should pass, secret redacted
    rec = {"api_key": "sk-1234567890abcdef", "content": "hello world", "nested": {"token": "Bearer abc"}}
    obj = ledger.append(rec)
    # returned obj's record should be redacted
    assert obj["record"].get("api_key") in ("***", "***REDACTED***")
    assert obj["record"].get("content") == "hello world"
    assert obj["record"]["nested"].get("token") in ("***", "***REDACTED***") or "***" in str(obj["record"]["nested"])
    # persisted line also redacted
    persisted = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[0])
    assert persisted["record"].get("api_key") in ("***", "***REDACTED***")
    assert "sk-123" not in (tmp_path / "ledger.jsonl").read_text()
    assert ledger.verify() is True


def test_ledger_result_sink_content_passthrough(tmp_path):
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    # tool_result type should allow content passthrough even if content looks like secret
    secret_content = "sk-1234567890abcdef"
    rec = {"type": "tool_result", "content": secret_content, "api_key": "sk-1234567890abcdef"}
    obj = ledger.append(rec)
    # content must remain, api_key redacted
    assert obj["record"].get("content") == secret_content
    assert obj["record"].get("api_key") in ("***", "***REDACTED***")


def test_write_tool_result_api_key_redacted_simple(tmp_path):
    """Minimal reproduction from task: write_tool_result({"api_key":"sk-123"}) 落盘后为 ***REDACTED***"""
    from hero_quant.agent.trace import TraceWriter
    from hero_quant.governance.ledger import Ledger

    # trace path
    w = TraceWriter(tmp_path / "trace.jsonl")
    w.append({"type": "tool_result", "content": "ok", "api_key": "sk-1234567890abcdef"})
    w.close()
    rec = json.loads((tmp_path / "trace.jsonl").read_text().strip().splitlines()[0])
    assert rec["api_key"] in ("***", "***REDACTED***")
    # ledger path
    ledger = Ledger(tmp_path / "ledger2.jsonl")
    ledger.append({"api_key": "sk-1234567890abcdef"})
    persisted = json.loads((tmp_path / "ledger2.jsonl").read_text().strip().splitlines()[0])
    assert persisted["record"]["api_key"] in ("***", "***REDACTED***")
