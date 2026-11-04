"""批次18 P2-1：数据源白名单与运行时注册数必须如实披露，禁止「16 源顺序回退」口径。"""

from __future__ import annotations


def test_registry_docstring_discloses_whitelist_vs_runtime_gap():
    import hero_quant.data.registry as reg

    doc = reg.__doc__ or ""
    assert "白名单" in doc, "docstring 必须说明 VALID_SOURCES 只是契约枚举"
    assert "运行时" in doc, "docstring 必须说明运行时实际注册数"
    assert "两源" in doc or "2 个" in doc, "docstring 必须写明运行时仅两源"
    assert "16 源顺序回退" not in doc


def test_runtime_registers_only_two_sources():
    """事实锁：白名单 16 个，_make_registry 实际只登记 Tencent + Yahoo。"""
    from hero_quant.data.sources import VALID_SOURCES
    from hero_quant.tools import market_data as md

    assert len(VALID_SOURCES) == 16
    registry = md._make_registry()
    with registry._loaders_lock:
        names = sorted(type(loader).__name__ for loader in registry._loaders)
    assert names == ["TencentLoader", "YahooLoader"], f"运行时注册源已变化: {names}"
