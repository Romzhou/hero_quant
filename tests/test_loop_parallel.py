import time
from hero_quant.agent.loop import AgentLoop
from hero_quant.tools.registry import TOOL_REGISTRY, tool


def test_loop_parallel():
    # warm-up: import policies and loop to exclude import cost from timed section
    from hero_quant.agent.policies import RetryPolicy  # noqa: F401

    _ = RetryPolicy()
    # warm up loop's lazy trace/context imports with a no-op run
    class _WarmLLM:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "warm"}

    AgentLoop(llm=_WarmLLM(), max_iterations=1).run("warmup")

    # cleanup prior runs
    for n in ["dummy_parallel_a", "dummy_parallel_b", "dummy_parallel_c"]:
        TOOL_REGISTRY.pop(n, None)

    @tool(name="dummy_parallel_a", description="dummy a", is_concurrency_safe=True)
    def dummy_parallel_a():
        time.sleep(0.2)
        return "a_ok"

    @tool(name="dummy_parallel_b", description="dummy b", is_concurrency_safe=True)
    def dummy_parallel_b():
        time.sleep(0.2)
        return "b_ok"

    @tool(name="dummy_parallel_c", description="dummy c", is_concurrency_safe=True)
    def dummy_parallel_c():
        time.sleep(0.2)
        return "c_ok"

    class FakeLLM:
        def stream_chat(self, goal):
            yield {
                "tool_calls": [
                    {"name": "dummy_parallel_a", "arguments": {}},
                    {"name": "dummy_parallel_b", "arguments": {}},
                    {"name": "dummy_parallel_c", "arguments": {}},
                ]
            }

    loop = AgentLoop(llm=FakeLLM(), max_iterations=3)
    start = time.perf_counter()
    result = loop.run("test parallel readonly")
    elapsed = time.perf_counter() - start

    # parallel 3*0.2 should be ~0.2s, serial would be 0.6s — assert <0.35s per spec
    assert elapsed < 0.35, f"parallel elapsed {elapsed:.3f}s >= 0.35s, not parallel"
    assert result.terminated is True
    # results should be in buffer
    assert "a_ok" in result.text
    assert "b_ok" in result.text
    assert "c_ok" in result.text

    # cleanup
    for n in ["dummy_parallel_a", "dummy_parallel_b", "dummy_parallel_c"]:
        TOOL_REGISTRY.pop(n, None)


def test_loop_parallel_write_tools_serial():
    """Write tools (is_concurrency_safe=False) remain serial — smoke check."""
    for n in ["dummy_write_a", "dummy_write_b"]:
        TOOL_REGISTRY.pop(n, None)

    order = []

    @tool(name="dummy_write_a", description="write a", is_concurrency_safe=False)
    def dummy_write_a():
        order.append("a_start")
        time.sleep(0.05)
        order.append("a_end")
        return "wa"

    @tool(name="dummy_write_b", description="write b", is_concurrency_safe=False)
    def dummy_write_b():
        order.append("b_start")
        time.sleep(0.05)
        order.append("b_end")
        return "wb"

    class FakeLLM:
        def stream_chat(self, goal):
            yield {
                "tool_calls": [
                    {"name": "dummy_write_a", "arguments": {}},
                    {"name": "dummy_write_b", "arguments": {}},
                ]
            }

    loop = AgentLoop(llm=FakeLLM(), max_iterations=3)
    result = loop.run("test write serial")
    assert result.terminated is True
    # serial order preserved
    assert order == ["a_start", "a_end", "b_start", "b_end"]

    for n in ["dummy_write_a", "dummy_write_b"]:
        TOOL_REGISTRY.pop(n, None)
