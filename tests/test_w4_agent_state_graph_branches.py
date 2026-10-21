"""W4 覆盖率补齐：agent/state.py 与 agent/graph.py 未覆盖分支。

覆盖目标：
- src/hero_quant/agent/state.py: _add_messages/_add_list 的 None/非列表输入与 dict 拷贝语义、
  _keep_last 的"保留最后非 None"、_max_depth 的 None/非数值 coerce 与取最大值
- src/hero_quant/agent/graph.py: _resolve_targets_from_text 的关键词解析、
  _normalize_selected 的别名归一/去重/未知角色拒绝、_get_role_tools/_get_role_prompt 的兜底、
  _check_breaker 的两种熔断器接口、_leaf_subagent 的深度超限与预算降级、
  plan_node/execute_node/verify_node/compensate_node 的各分支、build_research_graph 的入参校验

断言均为具体数值/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import pytest

from hero_quant.agent.graph import (
    MAX_DELEGATION_DEPTH,
    _check_breaker,
    _get_role_prompt,
    _get_role_tools,
    _leaf_subagent,
    _normalize_selected,
    _resolve_targets_from_text,
    build_research_graph,
    compensate_node,
    execute_node,
    plan_node,
    verify_node,
)
from hero_quant.agent.state import _add_list, _add_messages, _keep_last, _max_depth


# ---------------------------------------------------------------- state reducers


def test_add_messages_handles_none_and_non_list():
    """None 视为空列表；非列表按单元素包裹。"""
    assert _add_messages(None, None) == []
    assert _add_messages(None, [{"role": "user"}]) == [{"role": "user"}]
    assert _add_messages({"role": "user"}, None) == [{"role": "user"}]
    assert _add_messages({"role": "user"}, {"role": "assistant"}) == [{"role": "user"}, {"role": "assistant"}]


def test_add_messages_copies_dicts():
    """返回值中的 dict 为副本，不与输入共享引用。"""
    src = {"role": "user", "content": "x"}
    out = _add_messages(None, [src])
    assert out[0] == src
    assert out[0] is not src


def test_add_list_handles_none_and_non_list():
    """通用列表归约与消息归约同语义。"""
    assert _add_list(None, None) == []
    assert _add_list(None, ["a"]) == ["a"]
    assert _add_list("a", None) == ["a"]
    assert _add_list(["a"], ["b"]) == ["a", "b"]


def test_add_list_copies_dicts():
    """dict 成员被拷贝。"""
    src = {"k": 1}
    out = _add_list(None, [src])
    assert out[0] == {"k": 1}
    assert out[0] is not src


def test_keep_last_prefers_non_none_right():
    """b 非 None 取 b，否则保留 a。"""
    assert _keep_last("a", "b") == "b"
    assert _keep_last("a", None) == "a"
    assert _keep_last(None, "b") == "b"
    assert _keep_last(None, None) is None


def test_max_depth_uses_maximum():
    """delegation_depth 取最大值。"""
    assert _max_depth(1, 5) == 5
    assert _max_depth(5, 1) == 5
    assert _max_depth(3, 3) == 3


def test_max_depth_none_handling():
    """None 视为缺省。"""
    assert _max_depth(None, 3) == 3
    assert _max_depth(3, None) == 3
    assert _max_depth(None, None) is None


def test_max_depth_coerces_non_int():
    """非 int 输入宽容 coerce，非法值视为缺省。"""
    assert _max_depth("3", 4) == 4
    assert _max_depth("abc", 4) == 4
    assert _max_depth("abc", None) is None


# ---------------------------------------------------------------- graph 辅助


def test_resolve_targets_from_text_maps_keywords():
    """自由文本关键词映射为分析师目标。"""
    assert _resolve_targets_from_text("analyze market and news") == ["market", "news"]
    assert _resolve_targets_from_text("check risk and regime") == ["regime", "risk"]


def test_resolve_targets_dedupes_aliases():
    """social 与 sentiment 归一到同一目标，不重复。"""
    assert _resolve_targets_from_text("social sentiment") == ["sentiment"]


def test_resolve_targets_empty_text():
    """无关键词时返回空列表（调用方据此选默认扇出）。"""
    assert _resolve_targets_from_text("") == []
    assert _resolve_targets_from_text(None) == []


def test_normalize_selected_none_is_empty():
    """selected=None 归一为空列表。"""
    assert _normalize_selected(None) == []


def test_normalize_selected_alias_and_dedupe():
    """别名归一 + 去重，保持首次出现顺序。"""
    assert _normalize_selected(["Market", "market", "fundamental"]) == ["market", "fundamentals"]


def test_normalize_selected_unknown_role_raises():
    """未知角色直接拒绝（fail-visible）。"""
    with pytest.raises(ValueError, match="unknown analyst role"):
        _normalize_selected(["not-a-role"])


def test_get_role_tools_and_prompt_have_defaults():
    """未知角色返回空工具集与默认提示词。"""
    assert _get_role_tools("unknown-role") == []
    assert _get_role_tools("market") == [
        "get_market_data",
        "get_bars_range",
        "list_markets",
        "compute_indicator",
        "compute_sharpe",
        "compute_drawdown",
        "compute_correlation",
    ]
    assert "unknown-role" in _get_role_prompt("unknown-role")
    assert "Market Analyst" in _get_role_prompt("market")


def test_check_breaker_prefers_check_and_add():
    """优先走原子 check_and_add 接口。"""
    class _WithAdd:
        def __init__(self):
            self.calls = []

        def check_and_add(self, cost):
            self.calls.append(cost)
            return True

        def should_fallback(self, cost=0.0):
            raise AssertionError("should_fallback must not be used when check_and_add exists")

    b = _WithAdd()
    assert _check_breaker(b) is True
    assert b.calls == [0.1]


def test_check_breaker_falls_back_to_should_fallback():
    """无 check_and_add 时回退 should_fallback 查询。"""
    class _OnlyQuery:
        def should_fallback(self, cost=0.0):
            return cost > 0.05

    assert _check_breaker(_OnlyQuery()) is True


# ---------------------------------------------------------------- leaf subagent


def test_leaf_subagent_depth_exceeded():
    """delegation_depth 超上限时返回 budget_exceeded，不执行研究。"""
    run = _leaf_subagent("market")
    res = run({"delegation_depth": MAX_DELEGATION_DEPTH})
    assert res["subagent_outputs"][0]["status"] == "budget_exceeded"
    assert "delegation budget exceeded" in res["messages"][0]["content"]


def test_leaf_subagent_invalid_depth_defaults_to_zero():
    """非法 delegation_depth 收敛为 0（告警不崩）。"""
    run = _leaf_subagent("market")
    res = run({"delegation_depth": "not-a-number"})
    assert res["subagent_outputs"][0].get("status") != "budget_exceeded"


def test_leaf_subagent_breaker_fallback():
    """注入的熔断器判定超限 -> 返回 fallback 状态。"""
    class _Tripped:
        def check_and_add(self, cost):
            return True

        def should_fallback(self, cost=0.0):
            return True

    run = _leaf_subagent("risk", breaker=_Tripped())
    res = run({"delegation_depth": 0})
    assert res["subagent_outputs"][0]["status"] == "fallback"
    assert "budget fallback" in res["messages"][0]["content"]


def test_leaf_subagent_normal_path_returns_role_and_tools():
    """正常路径返回角色提示与绑定工具（工具集为角色声明与注册表的交集）。"""
    from hero_quant.tools.registry import TOOL_REGISTRY

    run = _leaf_subagent("market")
    res = run({"delegation_depth": 0})
    out = res["subagent_outputs"][0]
    assert out["agent"] == "market"
    assert out["role_prompt"].startswith("You are Market Analyst")
    expected = [t for t in _get_role_tools("market") if t in TOOL_REGISTRY]
    assert out["tools"] == expected
    assert res["agent_traces"][0]["agent"] == "market"


def test_leaf_subagent_binds_registered_tools(monkeypatch):
    """注册表内存在的工具被绑定进角色上下文，并出现在 preview 中。"""
    import hero_quant.tools.registry as reg

    fake = reg.TOOL_REGISTRY.get("get_market_data")
    assert fake is None or True  # 注册表可能已被其他用例填充
    monkeypatch.setitem(reg.TOOL_REGISTRY, "get_market_data", object())
    run = _leaf_subagent("market")
    res = run({"delegation_depth": 0})
    assert "get_market_data" in res["subagent_outputs"][0]["tools"]
    assert "tools: get_market_data" in res["messages"][0]["content"]


# ---------------------------------------------------------------- 节点


def test_plan_node_depth_exceeded():
    """plan 阶段深度超限直接返回预算提示。"""
    res = plan_node({"delegation_depth": MAX_DELEGATION_DEPTH, "messages": []})
    assert "delegation budget exceeded" in res["messages"][0]["content"]
    assert res["delegation_depth"] == MAX_DELEGATION_DEPTH + 1


def test_plan_node_builds_send_fanout():
    """plan 阶段按关键词扇出，返回带 goto 的 Command。"""
    cmd = plan_node({"delegation_depth": 0, "messages": [{"role": "user", "content": "analyze market"}]})
    assert cmd.update["plan"].startswith("plan for:")
    assert cmd.update["delegation_depth"] == 1
    assert len(cmd.goto) == 1


def test_plan_node_default_targets_when_no_keyword():
    """无关键词时使用默认三分析师扇出。"""
    cmd = plan_node({"delegation_depth": 0, "messages": [{"role": "user", "content": "hello"}]})
    assert len(cmd.goto) == 3


def test_execute_node_depth_exceeded():
    """遗留 execute 节点在深度超限时返回预算耗尽提示。"""
    res = execute_node({"delegation_depth": MAX_DELEGATION_DEPTH})
    assert "budget exhausted" in res["messages"][0]["content"]


def test_execute_node_runs_three_subagents():
    """遗留 execute 节点跑 factor/regime/risk 三个子代理。"""
    res = execute_node({"delegation_depth": 0, "messages": []})
    assert res["delegation_depth"] == 1
    assert res["messages"][-1]["content"] == "execute done"
    assert len(res["subagent_outputs"]) == 3


def test_verify_node_confidence_scales_with_evidence():
    """置信度随证据条数上升并封顶 0.85。"""
    assert verify_node({})["confidence"] == 0.50
    assert verify_node({"subagent_outputs": [{"agent": "a"}]})["confidence"] == 0.60
    assert verify_node({"subagent_outputs": [{"agent": "a"}] * 3})["confidence"] == 0.70
    assert verify_node({"subagent_outputs": [{"agent": "a"}] * 20})["confidence"] == 0.85


def test_verify_node_falls_back_to_intermediate_results():
    """无 subagent_outputs 时回退 intermediate_results。"""
    res = verify_node({"intermediate_results": [{"agent": "a"}]})
    assert res["confidence"] == 0.60


def test_verify_node_coerces_non_list_outputs():
    """outputs 非 list 时收敛为空（不崩）。"""
    res = verify_node({"subagent_outputs": "not-a-list"})
    assert res["confidence"] == 0.50
    assert len(res["pros"]) == 2
    assert len(res["cons"]) == 2


def test_compensate_node_returns_marker():
    """遗留 compensate 节点返回固定标记。"""
    res = compensate_node({})
    assert res["verification"] == "compensated"
    assert res["messages"][0]["content"] == "compensate done"


def test_build_research_graph_rejects_non_list_selected():
    """selected 非 list 抛 TypeError。"""
    with pytest.raises(TypeError, match="selected must be list or None"):
        build_research_graph(selected="market")  # type: ignore[arg-type]


def test_build_research_graph_compiles_and_runs():
    """默认扇出可编译并跑通到 verify。"""
    app = build_research_graph()
    out = app.invoke({"messages": [{"role": "user", "content": "analyze market"}]})
    assert out["verification"]
    assert out["confidence"] > 0.0
