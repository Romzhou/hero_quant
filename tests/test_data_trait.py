from hero_quant.data.trait import SourceTrait


def test_trait_registry_by_name():
    from hero_quant.data.registry import MarketDataRegistry

    r = MarketDataRegistry()
    # 新接口：get_bars通过trait分发
    assert hasattr(r, "register_trait")
    r.register_trait("test_src", SourceTrait)
    assert "test_src" in r.list_sources()
