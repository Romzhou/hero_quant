# tests/test_agent_loop.py
def test_agent_loop_terminates(monkeypatch):
    from hero_quant.agent.loop import AgentLoop
    # mock llm 返回一次工具调用后结束
    class FakeLLM:
        def stream_chat(self, *a, **kw): yield {"type":"text","text":"done"}
    loop = AgentLoop(llm=FakeLLM(), max_iterations=3)
    result = loop.run("测试")
    assert result.terminated is True


def test_agent_loop_token_limit_math_and_invalid_inputs(caplog):
    from hero_quant.agent.loop import AgentLoop, estimate_tokens
    import logging
    # token math: estimate_tokens handles None, bytes, list edge
    assert estimate_tokens(None) == 0
    assert estimate_tokens(b"hello world") == len("hello world") // 4
    assert estimate_tokens(["a", "b"]) > 0
    # invalid token_limit string coerced with warning and clamped
    with caplog.at_level(logging.WARNING):
        loop = AgentLoop(llm=type("LLM", (), {"stream_chat": lambda self, g: [{"type":"text","text":"hi"}]})(), token_limit="not-a-number")
        assert loop.token_limit == 60000
        assert any("token_limit" in r.message for r in caplog.records)
    # zero/negative token_limit treated as unlimited (None)
    loop2 = AgentLoop(llm=type("LLM", (), {"stream_chat": lambda self, g: [{"type":"text","text":"hi"}]})(), token_limit=0)
    assert loop2.token_limit is None
    loop3 = AgentLoop(llm=type("LLM", (), {"stream_chat": lambda self, g: [{"type":"text","text":"hi"}]})(), token_limit=-5)
    assert loop3.token_limit is None
    # invalid max_iterations
    with caplog.at_level(logging.WARNING):
        loop4 = AgentLoop(llm=type("LLM", (), {"stream_chat": lambda self, g: [{"type":"text","text":"hi"}]})(), max_iterations="bad")
        assert loop4.max_iterations == 5


def test_agent_loop_max_iterations_exhausted():
    from hero_quant.agent.loop import AgentLoop
    class FakeLLM:
        def stream_chat(self, g):
            yield {"type": "text", "text": ""}  # empty => continues until max_iterations
    loop = AgentLoop(llm=FakeLLM(), max_iterations=2)
    result = loop.run("test")
    assert result.reason == "max_iterations"
    assert result.terminated is True
    assert result.iterations == 2


def test_agent_loop_llm_error_and_timeout_reason():
    from hero_quant.agent.loop import AgentLoop
    class ErrLLM:
        def stream_chat(self, g):
            raise TimeoutError("timeout")
    loop = AgentLoop(llm=ErrLLM(), max_iterations=2)
    result = loop.run("goal")
    assert result.reason == "llm_timeout"
    assert "ERROR" in result.text or result.terminated
    class ErrLLM2:
        def stream_chat(self, g):
            raise ValueError("boom")
    loop2 = AgentLoop(llm=ErrLLM2(), max_iterations=2)
    result2 = loop2.run("goal2")
    assert result2.reason == "llm_error"
