# tests/test_registry_provenance.py
def test_registry_provenance_audit():
    from hero_quant.data.registry import MarketDataRegistry
    from hero_quant.data.loaders.tencent import TencentLoader
    reg=MarketDataRegistry(); reg.register(TencentLoader())
    bars,prov=reg.get_bars("600519.SH","1d","2026-08-01","2026-08-03")
    assert prov.source in ("tencent","synthetic")
    audit=reg.audit_log[-1]
    assert "symbol" in audit
