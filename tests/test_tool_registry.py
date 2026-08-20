def test_tool_registry_discovers():
    from hero_quant.tools.registry import tool, TOOL_REGISTRY
    @tool(name="demo_add", description="add")
    def demo_add(a: int, b: int) -> int: return a+b
    assert "demo_add" in TOOL_REGISTRY
    assert TOOL_REGISTRY["demo_add"].description == "add"
