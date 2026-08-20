# tests/test_tencent_live.py
def test_tencent_live_or_synthetic_flag(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE","synthetic")
    from hero_quant.data.loaders.tencent import TencentLoader
    import importlib, hero_quant.config.settings as s; importlib.reload(s)
    loader=TencentLoader()
    bars=loader.get_bars("600519.SH","1d","2026-08-01","2026-08-03")
    assert len(bars)>0
    assert bars[0]["close"]>0
