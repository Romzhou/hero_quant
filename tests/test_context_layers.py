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

    assert compacted.truncated is True
    assert "[COLLAPSED" in compacted.text
    assert compacted.text.startswith(original[:900])
    assert compacted.text.endswith(original[-500:])


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
    cm = ContextManager(max_chars=100)
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
