def test_trace_atomic_write(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    w = TraceWriter(tmp_path / "trace.jsonl", sidecar_threshold=50)
    w.append({"type": "llm", "content": "x" * 100})
    assert (tmp_path / "trace.jsonl").exists()
    # sidecar 文件存在且 trace 指向它
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    import json

    rec = json.loads(lines[0])
    assert "sidecar" in rec
    assert (tmp_path / rec["sidecar"]).exists()
    # sidecar 内容应包含原始大字段
    p = tmp_path / rec["sidecar"]
    text = p.read_text(encoding="utf-8")
    assert "x" * 100 in text
    # 防目录穿越
    from pathlib import Path

    assert TraceWriter._safe_sidecar_path(tmp_path, "../etc/passwd") is None
    # 正常路径应可解析
    assert TraceWriter._safe_sidecar_path(tmp_path, rec["sidecar"]) is not None
    w.close()
