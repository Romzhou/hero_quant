# tests/test_tool_contract.py
def test_tool_contract_schema_and_concurrency():
    from hero_quant.tools.registry import tool, TOOL_REGISTRY, get_definitions
    @tool(name="demo_safe", description="safe", parameters={"type":"object","properties":{"x":{"type":"string"}}, "required":["x"], "additionalProperties": False}, output={"type":"object","properties":{"ok":{"type":"boolean"}}}, is_concurrency_safe=lambda args: True)
    def f(x: str): return {"ok": True}
    assert TOOL_REGISTRY["demo_safe"].is_concurrency_safe({"x":"a"}) is True
    defs = get_definitions()
    assert any(d["function"]["name"]=="demo_safe" for d in defs)
