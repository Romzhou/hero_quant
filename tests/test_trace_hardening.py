# tests/test_trace_hardening.py
def test_trace_hard_threshold_and_hardlink(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    import json
    w = TraceWriter(tmp_path, sidecar_threshold=50000, hard_threshold=500)
    w.append({"type":"tool_result","tool":"get_bars","content":"x"*60000})
    lines = (tmp_path/"trace.jsonl").read_text().strip().splitlines()
    rec = json.loads(lines[0])
    assert "result_path" in rec and "preview" in rec
    assert (tmp_path/rec["result_path"]).exists()
    w2 = TraceWriter(tmp_path, sidecar_threshold=50000)
    w2.append({"type":"tool_result","tool":"get_bars","content":"x"*100})
    assert len((tmp_path/"trace.jsonl").read_text().strip().splitlines())==2
