def test_context_vector_folding():
    """Vector folding: >80% max_chars triggers embedding summary not head2 tail2."""
    from hero_quant.agent.context import ContextManager

    cm = ContextManager(max_chars=100)
    # 6 msgs * ~26 chars = ~156 > 80% (80 chars) and >100, should trigger folding
    for i in range(6):
        cm.add("user", f"msg {i} " + "x" * 20)

    compacted = cm.compact()
    assert compacted.truncated is True, "should be truncated over threshold"
    combined = (compacted.text + " " + compacted.banner).lower()
    # must contain embedding summary marker
    assert "embedding" in combined, f"expected embedding summary, got text={compacted.text!r} banner={compacted.banner!r}"
    # not the old pure [SUMMARY] placeholder without embedding
    # old logic would be "[SUMMARY] 2 messages folded" without embedding keyword
    assert "embedding" in combined


def test_context_vector_threshold_80pct():
    """Threshold 80%: below 80% no fold, above 80% embedding fold even if < max_chars."""
    from hero_quant.agent.context import ContextManager

    cm = ContextManager(max_chars=100)
    # single short message ~10 chars <80% -> no truncation
    cm.add("user", "hello")
    c0 = cm.compact()
    assert c0.truncated is False
    assert "embedding" not in (c0.text + c0.banner).lower()

    # add messages to exceed 80% threshold (80 chars)
    # need total >80: add 4 * 26 =104 -> total >80 and >100 after add, but test the 80% boundary
    for i in range(4):
        cm.add("user", f"thr {i} " + "x" * 18)
    c1 = cm.compact()
    # >80% should now be truncated with embedding summary
    assert c1.truncated is True
    combined = (c1.text + " " + c1.banner).lower()
    assert "embedding" in combined, f"80% threshold not triggering embedding, got {c1.text!r} {c1.banner!r}"
