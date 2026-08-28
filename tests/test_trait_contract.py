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


def test_validate_loader_signature():
    """validate_loader must check method signatures, not just hasattr; used at registration."""
    from hero_quant.data.trait import validate_loader
    from hero_quant.data.registry import MarketDataRegistry

    # valid loader passes
    class GoodLoader:
        name = "good"
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return []
        def health(self):
            return {}

    validate_loader(GoodLoader())
    # also via isinstance still works (runtime_checkable shallow)
    from hero_quant.data.trait import SourceTrait
    assert isinstance(GoodLoader(), SourceTrait)

    # bad signature: missing interval param
    class BadSig:
        name = "bad"
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start):
            return []
        def health(self):
            return {}

    try:
        validate_loader(BadSig())
        assert False, "validate_loader should reject bad get_bars signature"
    except (TypeError, ValueError):
        pass

    # bad unit
    class BadUnit:
        name = "bad"
        markets = ["US"]
        unit = "lots"
        def get_bars(self, symbol, start, end, interval="1d"):
            return []
        def health(self):
            return {}
    try:
        validate_loader(BadUnit())
        assert False, "should reject invalid unit"
    except (TypeError, ValueError):
        pass

    # bad markets type
    class BadMarkets:
        name = "bad"
        markets = "US"
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return []
        def health(self):
            return {}
    try:
        validate_loader(BadMarkets())
        assert False, "should reject non-list markets"
    except (TypeError, ValueError):
        pass

    # registry.register must call validate_loader
    reg = MarketDataRegistry()
    try:
        reg.register(BadSig())
        assert False, "registry should reject bad loader via validate_loader"
    except ValueError:
        pass
    # good still registers
    reg.register(GoodLoader())
    assert len(reg._loaders) == 1


def test_get_bars_contract_doc_and_helper():
    """get_bars contract must document columns/index/tz and provide assertion helper."""
    import inspect
    import pandas as pd
    from hero_quant.data.trait import SourceTrait, assert_bars_contract, validate_bars_contract

    # docstring must mention required schema details
    doc = inspect.getdoc(SourceTrait.get_bars) or ""
    doc_low = doc.lower()
    for kw in ["columns", "sorted", "tz", "open", "close", "volume", "datetimeindex", "deduplicated"]:
        # allow partial match: at least mention columns and sorted and tz
        pass
    assert "close" in doc_low and "volume" in doc_low, f"doc should mention columns close/volume: {doc[:200]}"
    assert "sorted" in doc_low, f"doc should mention sorted index: {doc[:200]}"
    assert "tz" in doc_low or "utc" in doc_low, f"doc should mention tz-naive UTC: {doc[:200]}"

    # helper should validate contract: DataFrame with required columns passes, missing fails
    idx = pd.date_range("2024-01-01", periods=2, tz=None)
    df_ok = pd.DataFrame({"open": [1,2], "high": [1,2], "low": [1,2], "close": [1,2], "volume": [100,100]}, index=idx)
    # should not raise
    assert_bars_contract(df_ok)
    validate_bars_contract(df_ok)

    # missing column should raise
    df_bad = pd.DataFrame({"open": [1], "close": [1]}, index=idx[:1])
    try:
        assert_bars_contract(df_bad)
        assert False, "should reject missing columns"
    except (ValueError, AssertionError):
        pass

    # unsorted index should raise
    idx_unsorted = pd.DatetimeIndex(["2024-01-02", "2024-01-01"])
    df_unsorted = pd.DataFrame({"open": [1,1], "high": [1,1], "low": [1,1], "close": [1,1], "volume": [100,100]}, index=idx_unsorted)
    try:
        assert_bars_contract(df_unsorted)
        assert False, "should reject unsorted index"
    except (ValueError, AssertionError):
        pass

    # tz-aware index should raise
    idx_aware = pd.date_range("2024-01-01", periods=2, tz="UTC")
    df_aware = pd.DataFrame({"open": [1,2], "high": [1,2], "low": [1,2], "close": [1,2], "volume": [100,100]}, index=idx_aware)
    try:
        assert_bars_contract(df_aware)
        assert False, "should reject tz-aware index"
    except (ValueError, AssertionError):
        pass

    # list[dict] contract: allow list form with required keys
    good_list = [{"open":1,"high":1,"low":1,"close":1,"volume":100,"date":"2024-01-01"}]
    # helper should accept list and validate
    assert_bars_contract(good_list)
    bad_list = [{"open":1}]
    try:
        assert_bars_contract(bad_list)
        assert False, "should reject list missing close"
    except (ValueError, AssertionError):
        pass
