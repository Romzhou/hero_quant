# tests/test_agent_loop.py
def test_agent_loop_terminates(monkeypatch):
    from hero_quant.agent.loop import AgentLoop
    # mock llm 返回一次工具调用后结束
    class FakeLLM:
        def stream_chat(self, *a, **kw): yield {"type":"text","text":"done"}
    loop = AgentLoop(llm=FakeLLM(), max_iterations=3)
    result = loop.run("测试")
    assert result.terminated is True
