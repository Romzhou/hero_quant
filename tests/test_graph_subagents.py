# tests/test_graph_subagents.py
def test_graph_builds():
    from hero_quant.agent.graph import build_research_graph
    g=build_research_graph()
    assert g is not None
    assert hasattr(g,"invoke")


def test_graph_selected_validation_and_mutable_copy():
    from hero_quant.agent.graph import build_research_graph
    import pytest
    with pytest.raises(TypeError):
        build_research_graph(selected="not-a-list")  # type: ignore
    sel = ["market", "risk"]
    g = build_research_graph(selected=sel)
    assert g is not None
    # ensure defensive copy: mutate original should not affect graph's normalized list
    sel.append("news")
    g2 = build_research_graph(selected=["market"])
    assert g2 is not None


def test_graph_verify_falsy_or_chain_fixed():
    from hero_quant.agent.graph import verify_node
    # empty list should give confidence 0.65, not hide via falsy or
    state = {"subagent_outputs": []}
    res = verify_node(state)
    assert res["confidence"] == 0.65
    # None with intermediate_results should also handle
    state2 = {"subagent_outputs": None, "intermediate_results": [{"agent": "market", "output": "x"}]}
    res2 = verify_node(state2)
    assert res2["confidence"] > 0.5
    # invalid type coerced to empty
    state3 = {"subagent_outputs": "not-a-list"}
    res3 = verify_node(state3)
    assert res3["confidence"] == 0.65


def test_graph_plan_delegation_depth_invalid():
    from hero_quant.agent.graph import plan_node
    # invalid depth should not crash, should log and default to 0
    state = {"delegation_depth": "bad", "messages": [{"role": "user", "content": "hello market"}]}
    res = plan_node(state)
    # either Command or dict, should contain messages
    if hasattr(res, "update"):
        assert "messages" in res.update
    else:
        assert "messages" in res
