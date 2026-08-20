def test_context_compact_marks_truncation():
    from hero_quant.agent.context import ContextManager
    cm = ContextManager(max_chars=100)
    for i in range(20): cm.add("user", f"msg {i} " + "x"*20)
    compacted = cm.compact()
    assert compacted.truncated is True
    assert "TRUNCATED" in compacted.banner
    # 必须保留首尾，不静默丢
    assert "msg 0" in compacted.text
    assert "msg 19" in compacted.text
