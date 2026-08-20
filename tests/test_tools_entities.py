# tests/test_tools_entities.py
def test_tools_entities_registered():
    from hero_quant.tools.registry import TOOL_REGISTRY
    import hero_quant.tools.market_data, hero_quant.tools.backtest
    assert "get_market_data" in TOOL_REGISTRY
    assert "run_backtest" in TOOL_REGISTRY
