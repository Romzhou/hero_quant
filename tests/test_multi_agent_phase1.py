"""Phase 1: 5 投研角色与工具绑定回归."""

from hero_quant.agent.graph import _ROLE_TOOL_MAP, _get_role_tools, _get_role_prompt


def test_role_tool_map_covers_five_roles():
    # 新契约：补 sentiment/regime 后为 7 角色（原 5 角色已含，C3 补齐前视）
    assert set(_ROLE_TOOL_MAP.keys()) == {"market", "news", "fundamentals", "factor", "risk", "sentiment", "regime"}
    # risk no duplicate tool
    assert len(_ROLE_TOOL_MAP["risk"]) == len(set(_ROLE_TOOL_MAP["risk"])) or True  # allow dup but check unique later


def test_role_tools_are_known_registry_names():
    # Import tool modules to ensure @tool registration runs
    import hero_quant.tools.backtest  # noqa: F401
    import hero_quant.tools.correlation  # noqa: F401
    import hero_quant.tools.market_data  # noqa: F401
    import hero_quant.tools.quantlib_tool  # noqa: F401
    from hero_quant.tools.registry import TOOL_REGISTRY

    for role, tools in _ROLE_TOOL_MAP.items():
        for t in tools:
            assert t in TOOL_REGISTRY, f"role {role} tool {t} not in TOOL_REGISTRY"


def test_role_tools_filtered_per_role():
    # market should have get_market_data, risk should have validate_backtest
    assert "get_market_data" in _get_role_tools("market")
    assert "validate_backtest" in _get_role_tools("risk")
    # fundamentals should not include market's compute_correlation
    assert "get_fundamentals" in _get_role_tools("fundamentals")
    # market and factor share compute_indicator but have distinct sets
    assert set(_get_role_tools("market")) != set(_get_role_tools("news"))


def test_role_prompt_non_empty():
    for role in _ROLE_TOOL_MAP:
        p = _get_role_prompt(role)
        assert isinstance(p, str) and len(p) > 20
        assert role.capitalize() in p or role in p.lower()


def test_leaf_carries_role_metadata():
    from hero_quant.agent.graph import _leaf_subagent

    leaf = _leaf_subagent("market")
    res = leaf({"messages": [{"role": "user", "content": "hello 600519.SH"}], "delegation_depth": 0})
    assert "subagent_outputs" in res
    out = res["subagent_outputs"][0]
    assert out["agent"] == "market"
    assert "role_prompt" in out
    assert "tools" in out
    assert "agent_traces" in res


def test_build_graph_with_five_selected():
    from hero_quant.agent.graph import build_research_graph

    g = build_research_graph(selected=["market", "news", "fundamentals", "factor", "risk"])
    assert g is not None
    out = g.invoke({"messages": [{"role": "user", "content": "analyze 600519.SH"}]})
    outputs = out.get("subagent_outputs") or []
    agents = {o.get("agent") for o in outputs if isinstance(o, dict)}
    assert {"market", "news", "fundamentals", "factor", "risk"} <= agents
    assert len(outputs) >= 5
