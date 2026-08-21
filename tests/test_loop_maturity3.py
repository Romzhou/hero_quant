"""TDD for A2-1 token estimate fix."""
from hero_quant.agent.loop import estimate_tokens, AgentLoop


def test_token_estimate():
    # 6000 chars -> 1500 tokens, old len(buffer) would return 6000
    buf = "a" * 6000
    assert estimate_tokens(buf) == 1500, f"expected 1500 got {estimate_tokens(buf)}"
    assert estimate_tokens(buf) != 6000, "old len(buffer) hallucination should not happen"
    # 240k chars -> 60000 tokens threshold
    assert estimate_tokens("a" * 240_000) == 60_000
    # empty
    assert estimate_tokens("") == 0
    # threshold behavior: token_limit=60000 should trigger at 240k chars, not 60k
    class FakeLLM60k:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "a" * 60_000}

    loop = AgentLoop(llm=FakeLLM60k(), max_iterations=2, token_limit=60000)
    result = loop.run("test")
    # 60k chars = 15k tokens, should NOT trigger token_limit
    assert result.reason != "token_limit", f"60k chars should not trigger token_limit, got {result.reason}"

    class FakeLLM240k:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "a" * 240_000}

    loop2 = AgentLoop(llm=FakeLLM240k(), max_iterations=2, token_limit=60000)
    result2 = loop2.run("test 240k")
    assert result2.reason == "token_limit"
    assert "TRUNCATED" in result2.text
    assert result2.token_count == estimate_tokens(result2.text) or result2.token_count == len(result2.text)//4
