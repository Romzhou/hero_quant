"""Task10 TDD：白名单、单源、缓存失效、合成比较器显式放行。"""

import pytest


def test_good_not_in_valid_sources_via_registry():
    """VALID_SOURCES 不含 good 后门；从 registry 单源导入。"""
    from hero_quant.data.registry import VALID_SOURCES

    assert "good" not in VALID_SOURCES
    assert "GOOD" not in [s.lower() for s in VALID_SOURCES]


def test_good_not_in_valid_sources_via_sources():
    """sources 单源不含 good。"""
    from hero_quant.data.sources import VALID_SOURCES

    assert "good" not in VALID_SOURCES


def test_good_not_in_valid_sources_via_trait_module():
    """trait 模块导出的 VALID_SOURCES 也不含 good（单源透出）。"""
    # trait 需透出模块级 VALID_SOURCES（从 sources 单源导入）
    from hero_quant.data.trait import VALID_SOURCES as trait_vs

    assert "good" not in trait_vs


def test_valid_sources_single_source_identity():
    """三处 VALID_SOURCES 必须同源（同一对象或值相等），避免双轨漂移。"""
    from hero_quant.data import VALID_SOURCES as pkg_vs
    from hero_quant.data.registry import VALID_SOURCES as reg_vs
    from hero_quant.data.sources import VALID_SOURCES as src_vs
    from hero_quant.data.trait import VALID_SOURCES as trait_vs

    # 值相等
    assert list(pkg_vs) == list(src_vs) == list(reg_vs) == list(trait_vs)
    # 推荐对象同一性（单源导入）
    assert pkg_vs is src_vs or pkg_vs == src_vs
    assert reg_vs is src_vs or reg_vs == src_vs
    assert trait_vs is src_vs or trait_vs == src_vs


def test_validate_loader_rejects_good():
    """validate_loader 对 name=good 必须 fail-closed 拒绝。"""
    from hero_quant.data.trait import validate_loader

    class GoodLoader:
        name = "good"
        markets = ["US"]
        unit = "shares"

        def get_bars(self, symbol, start, end, interval="1d"):
            return []

        def health(self):
            return {}

    with pytest.raises((ValueError, TypeError)):
        validate_loader(GoodLoader())


def test_validate_loader_rejects_good_source_field():
    """loader.source=good 同样拒绝。"""
    from hero_quant.data.trait import validate_loader

    class LoaderWithGoodSource:
        name = "synthetic"
        source = "good"
        markets = ["US"]
        unit = "shares"

        def get_bars(self, symbol, start, end, interval="1d"):
            return []

        def health(self):
            return {}

    with pytest.raises((ValueError, TypeError)):
        validate_loader(LoaderWithGoodSource())


def test_registry_register_rejects_good():
    """registry.register 对 good 同样拒绝（透传 validate_loader）。"""
    from hero_quant.data.registry import MarketDataRegistry

    class GoodLoader:
        name = "good"
        markets = ["US"]
        unit = "shares"

        def get_bars(self, symbol, start, end, interval="1d"):
            return []

        def health(self):
            return {}

    reg = MarketDataRegistry()
    with pytest.raises((ValueError, TypeError)):
        reg.register(GoodLoader())


def test_settings_cache_can_be_invalidated():
    """缓存可按用例失效：clear_settings_cache 与 force_refresh 生效。"""
    import os
    from hero_quant.data.registry import _get_data_mode, clear_settings_cache

    # 清理后切 synthetic/live 应分别可读，不应脏读
    clear_settings_cache()
    os.environ["HERO_DATA_MODE"] = "synthetic"
    clear_settings_cache()
    assert _get_data_mode() == "synthetic"

    os.environ["HERO_DATA_MODE"] = "live"
    # 不清缓存仍为 synthetic（脏读），需 force_refresh 或 clear 后才更新
    assert _get_data_mode() == "synthetic"  # 缓存未失效，仍为旧值
    assert _get_data_mode(force_refresh=True) == "live"

    clear_settings_cache()
    assert _get_data_mode() == "live"

    # 复位
    os.environ["HERO_DATA_MODE"] = "live"
    clear_settings_cache()


def test_conftest_autouse_clears_cache():
    """conftest 存在 autouse fixture 每用例清缓存（避免跨用例脏读）。"""
    import pathlib

    p = pathlib.Path("tests/conftest.py")
    # 也接受项目根 conftest
    alt = pathlib.Path("conftest.py")
    text = ""
    if p.exists():
        text = p.read_text(encoding="utf-8")
    elif alt.exists():
        text = alt.read_text(encoding="utf-8")
    else:
        pytest.fail("tests/conftest.py 不存在，需提供 autouse 清缓存 fixture")
    assert "clear_settings_cache" in text or "_settings_mode_cache" in text
    assert "autouse" in text


def test_synthetic_comparison_requires_flag():
    """合成比较器无 allow_synthetic_comparison 必须 raise CrossSourceError。"""
    import pandas as pd
    from hero_quant.data.registry import CrossSourceError, MarketDataRegistry, Provenance

    reg = MarketDataRegistry()

    class SynLoader:
        markets = ["US"]
        unit = "shares"
        source = "synthetic"
        name = "synthetic"

        def get_bars(self, symbol, start, end, interval="1d"):
            return pd.DataFrame({"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]}), Provenance(source="synthetic", unit="shares", symbol=symbol)

        def health(self):
            return {"status": "ok"}

    class LiveLoader:
        markets = ["US"]
        unit = "shares"
        source = "tencent"
        name = "tencent"

        def get_bars(self, symbol, start, end, interval="1d"):
            return pd.DataFrame({"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]}), Provenance(source="tencent", unit="shares", symbol=symbol)

        def health(self):
            return {"status": "ok"}

    reg.register(SynLoader())
    reg.register(LiveLoader())

    bars = pd.DataFrame({"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]})
    prov_syn = Provenance(source="synthetic", unit="shares", symbol="AAPL.US")

    # 无 flag 必须 raise
    with pytest.raises(CrossSourceError):
        reg._cross_source_check("AAPL.US", bars, prov_syn, "1d", "2024-01-01", "2024-01-02")

    # 显式 opt-in 后不再抛（仅跳过该 comparator，记录 warning）
    prov_optin = Provenance(source="synthetic", unit="shares", symbol="AAPL.US")
    prov_optin.allow_synthetic_comparison = True
    reg._cross_source_check("AAPL.US", bars, prov_optin, "1d", "2024-01-01", "2024-01-02")

    # live prov 但对照为 synthetic 也需 flag
    prov_live = Provenance(source="tencent", unit="shares", symbol="AAPL.US")
    with pytest.raises(CrossSourceError):
        reg._cross_source_check("AAPL.US", bars, prov_live, "1d", "2024-01-01", "2024-01-02")
