# tests/test_fc_formatting.py
def test_fc_truncate_and_redact():
    from hero_quant.config.limits import TOOL_RESULT_LIMIT
    assert TOOL_RESULT_LIMIT==10000
    from hero_quant.tools.redaction import redact_tool_result
    big = "a"*15000
    res = redact_tool_result(big)
    assert "TRUNCATED" in res or len(res) <= 10000
