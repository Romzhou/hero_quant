import pytest, importlib, unittest.mock as mock, json

def _reload_settings(monkeypatch, mode):
    monkeypatch.setenv("HERO_DATA_MODE", mode)
    import hero_quant.config.settings as s
    importlib.reload(s)
    return s

def test_tencent_https_and_quoted_symbol(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader
    loader = TencentLoader()
    captured = {}
    def fake_urlopen(url, timeout=2):
        captured["url"] = url
        data = {"data": {"sh600519": {"day": [["2025-01-01", 10, 11, 12, 9, 100]]}}}
        text = json.dumps(data).encode()
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = text
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *args: False
        return mock_resp
    with mock.patch("hero_quant.data.loaders.tencent.time.sleep"):
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            symbol = "600519.SH; rm -rf"
            try:
                loader.get_bars(symbol, "2025-01-01", "2025-01-05")
            except Exception:
                pass
            url = captured.get("url", "")
            assert url.startswith("https://"), f"url must be https, got {url}"
            captured.clear()
            symbol2 = "600519.SH;evil"
            try:
                loader.get_bars(symbol2, "2025-01-01", "2025-01-05")
            except Exception:
                pass
            url2 = captured.get("url", "")
            assert ";evil" not in url2 or "%3B" in url2, f"symbol not quoted: {url2}"

def test_tencent_falsy_zero_preserved(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader
    loader = TencentLoader()
    def fake_urlopen(url, timeout=2):
        data = {"data": {"sz000001": {"day": [["2025-01-01", 0, 0, 0, 0, 0]]}}}
        text = json.dumps(data).encode()
        m = mock.MagicMock()
        m.read.return_value = text
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *args: False
        return m
    with mock.patch("hero_quant.data.loaders.tencent.time.sleep"):
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            bars = loader.get_bars("000001.SZ", "2025-01-01", "2025-01-05")
            assert bars[0]["open"] == 0.0, f"0 price should be preserved, got {bars[0]['open']}"
            assert bars[0]["volume"] == 0.0, f"0 volume should be preserved, got {bars[0]['volume']}"

def test_tencent_dict_falsy_zero_preserved(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader
    loader = TencentLoader()
    def fake_urlopen_dict(url, timeout=2):
        inner = [{"date": "2025-01-01", "open": 0, "close": 0, "high": 0, "low": 0, "volume": 0}]
        data = {"data": {"k1": {"inner_key": inner}}}
        text = json.dumps(data).encode()
        m = mock.MagicMock()
        m.read.return_value = text
        m.__enter__ = lambda self: self
        m.__exit__ = lambda self, *args: False
        return m
    with mock.patch("hero_quant.data.loaders.tencent.time.sleep"):
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen_dict):
            bars = loader.get_bars("000001.SZ", "2025-01-01", "2025-01-05")
            assert bars[0]["open"] == 0.0
            assert bars[0]["volume"] == 0.0

def test_tencent_synthetic_mode_fallback(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader
    loader = TencentLoader()
    bars = loader.get_bars("000001.SZ", "2025-01-01", "2025-01-05")
    assert len(bars) > 0
