import inspect


def test_trait_contract_signature_consistent():
    from hero_quant.data.trait import SourceTrait
    from hero_quant.data.loaders.akshare_loader import AKShareLoader

    sig_trait = inspect.signature(SourceTrait.get_bars)
    sig_loader = inspect.signature(AKShareLoader.get_bars)

    # 参数名必须一致：symbol, start, end, interval
    assert list(sig_trait.parameters.keys()) == list(sig_loader.parameters.keys()), (
        f"trait {list(sig_trait.parameters.keys())} vs loader {list(sig_loader.parameters.keys())}"
    )
    # 进一步校验顺序与默认值
    assert list(sig_trait.parameters.keys()) == ["self", "symbol", "start", "end", "interval"]
    assert sig_loader.parameters["interval"].default == "1d"


def test_tencent_yahoo_align_trait():
    import inspect
    from hero_quant.data.trait import SourceTrait
    from hero_quant.data.loaders.tencent import TencentLoader
    from hero_quant.data.loaders.yahoo import YahooLoader

    trait_params = list(inspect.signature(SourceTrait.get_bars).parameters.keys())
    for Loader in (TencentLoader, YahooLoader):
        loader_params = list(inspect.signature(Loader.get_bars).parameters.keys())
        assert loader_params == trait_params, f"{Loader.__name__} {loader_params} != {trait_params}"


def test_registry_calls_trait_order():
    import inspect
    from hero_quant.data.registry import MarketDataRegistry

    sig = inspect.signature(MarketDataRegistry.get_bars)
    # registry 统一为 trait 顺序
    assert list(sig.parameters.keys()) == ["self", "symbol", "start", "end", "interval"]
    assert sig.parameters["interval"].default == "1d"
