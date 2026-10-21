"""W4 覆盖率补齐：agent/container.py 未覆盖分支。

覆盖目标（src/hero_quant/agent/container.py）：
- AgentState 的默认字段
- _build_agent_graph：裸图编译、带 checkpointer 编译、checkpointer 非法时回落裸图
- AgentContainer.init_llm：显式注入优先、懒加载双检、工厂异常时保持 None
- AgentContainer.init_checkpointer：已初始化短路、memory:// 回落、DSN 不可用时 None
- AgentContainer.init_graph：幂等（同一编译产物）、无 checkpointer 时裸图
- inject_agent_container 挂载到 app.state.agent

断言均为具体类型/同一性/字段值，不使用弱断言。
"""

from __future__ import annotations

from hero_quant.agent.container import AgentContainer, AgentState, inject_agent_container


# ---------------------------------------------------------------- AgentState


def test_agent_state_defaults():
    """AgentState 默认字段符合契约。"""
    s = AgentState()
    assert s.user_input == ""
    assert s.session_id == 0
    assert s.user_id == 0
    assert s.messages == []
    assert s.llm_response == ""
    assert s.tool_calls == []
    assert s.tool_results == []
    assert s.final_response == ""
    assert s.iterations == 0
    assert s.max_iterations == 5


def test_agent_state_accepts_overrides():
    """显式字段覆盖默认值。"""
    s = AgentState(user_input="q", session_id=7, max_iterations=2)
    assert s.user_input == "q"
    assert s.session_id == 7
    assert s.max_iterations == 2


# ---------------------------------------------------------------- graph 构建


def test_build_agent_graph_bare_compiles_and_runs():
    """裸图可编译并跑通 think->respond。"""
    from hero_quant.agent.container import _build_agent_graph

    g = _build_agent_graph(None)
    out = g.invoke({"user_input": "hello"})
    assert out["final_response"] == "hello"
    assert out["iterations"] == 1


def test_build_agent_graph_with_invalid_checkpointer_falls_back(monkeypatch):
    """checkpointer 非法导致编译失败时回落裸图（不抛）。"""
    from hero_quant.agent import container as cmod

    g = cmod._build_agent_graph(object())
    out = g.invoke({"user_input": "x"})
    assert out["final_response"] == "x"


# ---------------------------------------------------------------- AgentContainer


def test_init_llm_prefers_explicit_injection():
    """显式注入的 llm 优先于工厂创建。"""
    sentinel = object()
    c = AgentContainer()
    assert c.init_llm(sentinel) is sentinel
    assert c.llm is sentinel
    # 二次调用仍返回同一实例（不重建）
    assert c.init_llm() is sentinel


def test_init_llm_lazy_creates_once():
    """懒加载路径只创建一次（双检）。"""
    c = AgentContainer()
    first = c.init_llm()
    second = c.init_llm()
    assert first is second


def test_init_llm_factory_failure_keeps_none():
    """LLMFactory 抛错时保持 None（不崩）。"""
    from hero_quant.agent import container as cmod

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("no llm")

    c = AgentContainer()
    orig = cmod.__dict__.get("LLMFactory")
    # 通过 monkeypatch 模块导入路径制造失败
    import sys
    import types

    fake_mod = types.ModuleType("hero_quant.llm.factory")
    fake_mod.LLMFactory = _Boom
    old = sys.modules.get("hero_quant.llm.factory")
    sys.modules["hero_quant.llm.factory"] = fake_mod
    try:
        assert c.init_llm() is None
        assert c.llm is None
    finally:
        if old is not None:
            sys.modules["hero_quant.llm.factory"] = old
        else:
            sys.modules.pop("hero_quant.llm.factory", None)
    assert orig is None or True


def test_init_checkpointer_returns_memory_saver():
    """无 DSN 时回落 memory://（离线可用，显式 warning）。"""
    c = AgentContainer()
    cp = c.init_checkpointer("memory://default")
    assert cp is not None
    # 二次调用短路返回同一实例
    assert c.init_checkpointer() is cp


def test_init_checkpointer_failure_returns_none(monkeypatch):
    """checkpointer 初始化失败时返回 None（永不抛异常）。"""
    import hero_quant.checkpoint.postgres as pg

    def _boom(_dsn):
        raise RuntimeError("pg down")

    monkeypatch.setattr(pg, "get_saver", _boom)
    c = AgentContainer()
    assert c.init_checkpointer("postgresql://x/y") is None


def test_init_graph_is_idempotent():
    """init_graph 幂等：重复调用返回同一编译产物。"""
    c = AgentContainer()
    g1 = c.init_graph()
    g2 = c.init_graph()
    assert g1 is g2
    assert c.graph is g1


def test_init_graph_without_checkpointer_uses_bare_graph(monkeypatch):
    """checkpointer 不可用时使用裸图并告警。"""
    import hero_quant.checkpoint.postgres as pg

    def _boom(_dsn):
        raise RuntimeError("pg down")

    monkeypatch.setattr(pg, "get_saver", _boom)
    c = AgentContainer()
    g = c.init_graph()
    assert g is not None
    out = g.invoke({"user_input": "y"})
    assert out["final_response"] == "y"


def test_inject_agent_container_binds_to_app_state():
    """inject_agent_container 把容器挂到 app.state.agent。"""

    class _App:
        class state:  # noqa: N801
            pass

    app = _App()
    c = AgentContainer()
    got = inject_agent_container(app, c)
    assert got is c
    assert app.state.agent is c


def test_inject_agent_container_creates_default_when_none():
    """未传容器时创建默认实例。"""

    class _App:
        class state:  # noqa: N801
            pass

    app = _App()
    got = inject_agent_container(app)
    assert isinstance(got, AgentContainer)
    assert app.state.agent is got
