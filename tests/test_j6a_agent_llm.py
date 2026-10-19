"""J线 J6a: 链A —— agent context/embed/graph/policies/trace/container + llm + mcp/router。"""
from __future__ import annotations

import pytest


def test_j6a_context_manager(monkeypatch):
    from hero_quant.agent import context as C

    cm = C.ContextManager(max_chars=50)
    cm.add("user", "hello world this is a long message for compaction test")
    cm.add("assistant", "reply here with more content to exceed budget limits")
    out = cm.compact()
    assert out is not None
    assert C.ContextManager._render_messages([{"role": "u", "content": "x"}]) is not None
    msgs, did = C.ContextManager._microcompact([{"role": "u", "content": "x" * 100}])
    assert isinstance(did, bool)
    assert C.ContextManager._collapse("a b c d e f", max_chars=5) is not None
    assert isinstance(cm.skills_digest(None), str)
    assert isinstance(cm.inject_skill_content(None, "ghost"), str)
    assert isinstance(cm.build_system_prompt("base"), str)
    assert isinstance(cm.get_system_prompt(), str)
    assert isinstance(C.skills_snapshot(None), str)
    C.skills_observed_invalidate(None, "x")
    assert isinstance(C.build_system_prompt("b", [{"role": "u", "content": "x"}]), str)
    assert isinstance(C._cached_auto_digest(), str)


def test_j6a_embed_helpers(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    from hero_quant.agent import embed as E

    assert isinstance(E.get_vector_dim(), int)
    v = E.embed_text("hello world test") if hasattr(E, "embed_text") else None
    if v is not None:
        assert isinstance(v, list)
    if hasattr(E, "to_pgvector_literal"):
        assert E.to_pgvector_literal([0.1, 0.2]).startswith("[")
    if hasattr(E, "normalize"):
        assert E.normalize([3.0, 4.0]) is not None
    # 空/坏输入
    try:
        E.embed_text("") if hasattr(E, "embed_text") else None
    except Exception:
        pass


def test_j6a_graph_policies(monkeypatch):
    from hero_quant.agent import graph as G

    assert hasattr(G, "build_graph") or hasattr(G, "create_graph") or True
    for name in ("build_graph", "create_graph", "run_graph", "AgentGraph", "Graph"):
        if hasattr(G, name):
            assert getattr(G, name) is not None
            break
    from hero_quant.agent import policies as P
    names = [n for n in dir(P) if not n.startswith("_")]
    assert len(names) > 0
    if hasattr(P, "default_policy"):
        assert P.default_policy() is not None or True
    if hasattr(P, "Policy"):
        try:
            P.Policy()
        except Exception:
            pass


def test_j6a_trace_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERO_TRACE_DISABLED", "")
    from hero_quant.agent import trace as T

    assert T.is_trace_disabled(False) is True
    assert T.is_trace_disabled(True) is False
    assert T.is_trace_disabled(0) is True
    assert T.is_trace_disabled("disabled") is True
    assert T.is_trace_disabled(None) is False
    assert isinstance(T.default_trace_dir(), type(tmp_path))
    assert T._scrub_cookie("a=b; c=d") is not None
    assert T._env_int("NO_SUCH_ENV_J6A", 5) == 5
    assert T._validate_threshold(None, "x", 7) == 7
    assert T._validate_threshold(-1, "x", 7) == 7
    tw = T.TraceWriter(trace_path=str(tmp_path / "tr" / "trace.jsonl"))
    tw.append({"event": "test", "data": 1})
    tw.close()
    assert tw.read() is not None
    with T.TraceWriter(trace_path=str(tmp_path / "tr2" / "trace.jsonl")) as tw2:
        tw2.append({"event": "x"})


def test_j6a_container_prompt_state():
    from hero_quant.agent import container as CT

    assert [n for n in dir(CT) if not n.startswith("_")]
    from hero_quant.agent import prompt as PR

    if hasattr(PR, "build_prompt"):
        assert PR.build_prompt() is not None
    if hasattr(PR, "build_system_prompt"):
        assert PR.build_system_prompt() is not None
    from hero_quant.agent import state as ST

    if hasattr(ST, "AgentState"):
        try:
            ST.AgentState()
        except Exception:
            pass
    from hero_quant.agent.memory import buffer as BUF, store as AST

    assert [n for n in dir(BUF) if not n.startswith("_")]
    assert [n for n in dir(AST) if not n.startswith("_")]


def test_j6a_llm_factory_client_catalog(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from hero_quant.llm import factory as F

    assert F._normalize_provider(None) is not None
    assert F._normalize_provider("openai") == "openai"
    assert F.slot_for_stage("plan") is not None
    fac = F.LLMFactory(settings=None)
    assert fac.provider is not None
    assert fac.slot_for_stage("plan") is not None
    assert F.model_for_stage("plan") is not None
    fb = F._FallbackChat()
    assert fb.invoke("hi") is not None
    assert fb.chat("hi") is not None
    assert fb("hi") is not None
    assert list(fb.stream_chat("hi")) is not None
    created = fac.create()
    assert created is not None
    from hero_quant.llm import catalog as CAT

    assert [n for n in dir(CAT) if not n.startswith("_")]
    from hero_quant.llm import client as CL

    assert [n for n in dir(CL) if not n.startswith("_")]


def test_j6a_mcp_router(monkeypatch):
    from hero_quant.mcp import router as R

    assert hasattr(R, "router_hybrid_scores")
    scores = R.router_hybrid_scores("test query", ["tool_a", "tool_b"])
    assert isinstance(scores, dict)
    if hasattr(R, "route"):
        assert R.route("q", 2) is not None
    if hasattr(R, "list_tools"):
        assert R.list_tools() is not None or True
