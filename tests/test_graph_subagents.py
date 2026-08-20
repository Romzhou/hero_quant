# tests/test_graph_subagents.py
def test_graph_builds():
    from hero_quant.agent.graph import build_research_graph
    g=build_research_graph()
    assert g is not None
    assert hasattr(g,"invoke")
