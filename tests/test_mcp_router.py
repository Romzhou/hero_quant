def test_mcp_router_topk():
    from hero_quant.mcp.router import route
    tools = route("find momentum factors for 600519", k=5)
    assert len(tools) == 5 and "compute_factor" in tools
