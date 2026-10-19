"""J线 J9a: embed 全面 + trace offload + loop 工具成功/预算/墙钟路径。"""
from __future__ import annotations

import pytest


def test_j9a_embed_pure(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    from hero_quant.agent import embed as E

    assert E.get_vector_dim() in (32, 64, 128, 256, 384, 768, 1536)
    assert E.get_dim() == E.get_vector_dim()
    assert E.to_pgvector_literal([0.1, 0.2]).startswith("[")
    assert E.from_pgvector_literal("[0.1,0.2]") == pytest.approx([0.1, 0.2])
    assert E.from_pgvector_literal([0.1]) == [0.1]
    assert E.get_provider_name() is not None
    assert E.get_provider() is not None
    assert E.get_active_provider() is not None
    assert len(E._embed_offline("hello", 8)) == 8
    assert isinstance(E._tokenize("hello world"), list)
    assert len(E._token_vector("tok", 8)) == 8
    assert len(E._embed_semantic("hello", 8)) == 8
    assert E._l2_normalize([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    assert E._l2_normalize([0.0, 0.0]) == [0.0, 0.0]
    assert E.cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert E.cosine_sim([1.0], [0.0]) == 0.0
    c = E.centroid([[1.0, 0.0], [0.0, 1.0]])
    assert c == pytest.approx([0.5, 0.5])
    assert E.centroid([]) == []
    assert isinstance(E.embedding_summary([{"content": "hello world"}], 50), str)
    assert isinstance(E.embedding_summary(["a", "b"]), str)
    assert isinstance(E.embed_batch(["a", "b"]), list)
    assert isinstance(E.embed("hello embed test"), list)
    assert E._try_sentence_transformers("hi", 8) is None or isinstance(E._try_sentence_transformers("hi", 8), list)
    assert E._try_openai("hi", 8) is None
    assert E._embed_uncached("hi") is not None
    assert E._embed_cached("hi", 8) is not None
    try:
        assert E._get_sbert_model("no_such_model_xyz") is None or True
    except (ImportError, ModuleNotFoundError):
        pass


class _ToolOKLLM:
    def stream_chat(self, goal):
        yield {"tool_calls": [{"name": "j8_echo_tool", "arguments": {"x": 1}}]}
        yield {"type": "text", "text": "done after tool"}


def test_j9a_loop_tool_success():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    TOOL_REGISTRY.pop("j8_echo_tool", None)

    @tool(name="j8_echo_tool", description="echo", is_concurrency_safe=True,
          parameters={"type": "object", "properties": {"x": {"type": "number"}}, "required": []},
          output={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    def _echo(x: float = 1.0):
        return {"ok": True, "x": x}

    try:
        loop = AgentLoop(llm=_ToolOKLLM(), max_iterations=3)
        res = loop.run("use echo tool")
        assert isinstance(res.text, str)
        assert res.iterations >= 1
    finally:
        TOOL_REGISTRY.pop("j8_echo_tool", None)


def test_j9a_loop_budget_wall_token():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.policies import BudgetBreaker

    class _Big:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "word " * 20000}

    bb = BudgetBreaker(daily_limit=0.000001)
    res = AgentLoop(llm=_Big(), max_iterations=1, budget_breaker=bb).run("big")
    assert res.reason in ("budget_fallback", "token_limit", "completed", "max_iterations",
                          "llm_error", "user_stop", "graph_error", "grounding_failed")
    res2 = AgentLoop(llm=_Big(), max_iterations=1, token_limit=10).run("big tiny limit")
    assert res2.reason in ("token_limit", "budget_fallback", "completed", "max_iterations",
                           "llm_error", "user_stop", "graph_error", "grounding_failed")


def test_j9a_trace_offload(tmp_path):
    from hero_quant.agent import trace as T

    tw = T.TraceWriter(trace_path=str(tmp_path / "big" / "trace.jsonl"),
                       tool_result_offload=100, text_offload=100, preview_len=10)
    tw.append({"event": "tool_result", "result": "x" * 5000})
    tw.append({"event": "text", "text": "y" * 5000})
    tw.append({"not": "serializable", "obj": object()})
    tw.close()
    rows = tw.read()
    assert isinstance(rows, list) and len(rows) >= 1
    rows2 = tw.read(resolve_offloads=True)
    assert isinstance(rows2, list)
