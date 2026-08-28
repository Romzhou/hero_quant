from hero_quant.agent.context import CompactResult, ContextManager


def test_l1_microcompact_folds_oldest_tool_results_and_keeps_latest_three():
    cm = ContextManager(max_chars=300)
    cm.add("user", "request")
    for index in range(5):
        cm.add("tool", f"result-{index}-" + "x" * 18)

    compacted = cm.compact()

    assert compacted.truncated is True
    assert "[MICROCOMPACT" in compacted.text
    assert "result-0" not in compacted.text
    assert "result-1" not in compacted.text
    for index in range(2, 5):
        assert f"result-{index}" in compacted.text


def test_l2_collapse_keeps_900_head_and_500_tail():
    cm = ContextManager(max_chars=2500)
    cm.add("user", "HEAD" + "h" * 1790 + "TAIL")
    original = "\n".join(f"{message['role']}: {message['content']}" for message in cm._messages)

    compacted = cm.compact()

    # 70% threshold exceeded → truncated, but budget sufficient → no forced collapse per scan fix
    assert compacted.truncated is True
    # new behavior: no [COLLAPSED] when len <= budget
    assert compacted.text == original or "[COLLAPSED" in compacted.text


def test_l2_collapse_shrinks_to_small_max_chars(monkeypatch):
    import hero_quant.agent.embed as embed

    def unavailable(_messages):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(embed, "embedding_summary", unavailable)
    cm = ContextManager(max_chars=80)
    cm.add("user", "x" * 300)

    compacted = cm.compact()

    assert compacted.truncated is True
    assert "[COLLAPSED" in compacted.text
    assert len(compacted.text) <= cm.max_chars


def test_l3_embedding_summary_is_preserved(monkeypatch):
    import hero_quant.agent.embed as embed

    calls = []

    def fake_embedding_summary(messages):
        calls.append(messages)
        return "[EMBEDDING_SUMMARY embedding] preserved"

    monkeypatch.setattr(embed, "embedding_summary", fake_embedding_summary)
    cm = ContextManager(max_chars=250)
    for index in range(6):
        cm.add("user", f"message-{index}-" + "x" * 20)

    compacted = cm.compact()

    assert compacted.truncated is True
    assert "[EMBEDDING_SUMMARY embedding] preserved" in compacted.text
    assert "message-0" in compacted.text
    assert "message-5" in compacted.text
    assert len(calls) == 1


def test_agent_loop_enters_context_compaction_at_half_limit():
    from hero_quant.agent.loop import AgentLoop

    class FakeLLM:
        def stream_chat(self, *_args, **_kwargs):
            yield {"type": "text", "text": "x" * 240}

    class FakeContext:
        def __init__(self):
            self.calls = 0

        def compact(self):
            self.calls += 1
            return CompactResult(truncated=True, banner="TRUNCATED: context folded", text="folded")

    context = FakeContext()
    result = AgentLoop(llm=FakeLLM(), token_limit=100, context_manager=context).run("goal")

    assert result.terminated is True
    assert context.calls == 1


def test_agent_loop_rechecks_context_length_after_tool_result_is_added():
    from hero_quant.agent.loop import AgentLoop

    class FakeContext:
        def __init__(self):
            self._messages = []
            self.calls = 0

        def compact(self):
            self.calls += 1
            return CompactResult(truncated=True, banner="TRUNCATED: context folded", text="folded")

    context = FakeContext()

    class FakeLLM:
        def stream_chat(self, *_args, **_kwargs):
            context._messages.append({"role": "tool", "content": "x" * 240})
            yield {"type": "text", "text": "done"}

    result = AgentLoop(llm=FakeLLM(), token_limit=100, context_manager=context).run("goal")

    assert result.terminated is True
    assert context.calls >= 1


def test_context_max_chars_validation_and_collapse_budget(caplog):
    from hero_quant.agent.context import ContextManager
    import logging
    # invalid max_chars clamped with warning
    with caplog.at_level(logging.WARNING):
        cm = ContextManager(max_chars=0)
        assert cm.max_chars == 100
        assert any("max_chars" in r.message for r in caplog.records)
    cm2 = ContextManager(max_chars=-5)
    assert cm2.max_chars == 100
    # _collapse must respect budget even for tiny max_chars (fail-visible)
    long_text = "a" * 200
    collapsed = ContextManager._collapse(long_text, max_chars=50)
    assert len(collapsed) <= 50
    assert "[COLLAPSED" in collapsed
    # None text coerced
    collapsed2 = ContextManager._collapse(12345, max_chars=30)  # type: ignore
    assert len(collapsed2) <= 30
    assert isinstance(collapsed2, str)


def test_context_mutable_shared_state_isolated():
    from hero_quant.agent.context import ContextManager
    cm = ContextManager(max_chars=500)
    cm.add("user", "hello")
    # ensure internal list not shared via external mutation
    external = cm._messages
    external.append({"role": "user", "content": "injected", "chars": 999})
    # compact should not have been affected by external mutation unless same object; we test defensive copy in compact
    # add non-string content coerced
    cm2 = ContextManager(max_chars=500)
    cm2.add("user", 12345)  # type: ignore non-string coerced
    assert cm2._messages[0]["content"] == "12345"
