def test_yahoo_loader_declares_unit():
    from hero_quant.data.loaders.yahoo import YahooLoader
    y = YahooLoader()
    assert y.markets == ["US"]
    assert y.unit == "shares"
