"""J线 J20: 缓冲垫 —— tencent 全路径/settings 失败/heartbeat/loader 残余。"""
from __future__ import annotations

import json

import pytest


def test_j20_tencent_paths(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    import urllib.request as _url
    from hero_quant.data.loaders.tencent import DataValidationError, TencentLoader

    loader = TencentLoader()
    monkeypatch.setattr("hero_quant.data.loaders.tencent.TencentLoader._rate_limit", lambda self: None)

    def _resp_for(payload):
        raw = json.dumps(payload).encode()

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return raw
        return _R()

    # 无后缀品种 + qfqday 缺席回退扫描 + list entry
    payload = {"data": {"other": {"info": "meta"},
                        "scanme": {"qfqday": [["2024-01-01", "10", "10.5", "11", "9", "100"]]}}}
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _resp_for(payload))
    bars = loader._fetch_live_bars("600519", "2024-01-01", "2024-01-01")
    assert len(bars) == 1
    # entry 为 list 形态
    payload2 = {"data": {"600519": [["2024-01-01", "10", "10.5", "11", "9", "100"]]}}
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _resp_for(payload2))
    assert len(loader._fetch_live_bars("600519.SH", "2024-01-01", "2024-01-01")) == 1
    # 网络失败 -> RuntimeError
    def _boom(*a, **k):
        raise OSError("net down")
    monkeypatch.setattr(_url, "urlopen", _boom)
    with pytest.raises(RuntimeError):
        loader._fetch_live_bars("000001.SZ", "2024-01-01", "2024-01-02")
    # 空 qfqday -> ValueError->RuntimeError
    payload3 = {"data": {"sz000001": {"qfqday": []}}}
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _resp_for(payload3))
    with pytest.raises(RuntimeError):
        loader._fetch_live_bars("000001.SZ", "2024-01-01", "2024-01-02")


def test_j20_settings_fail_three_loaders(monkeypatch):
    import hero_quant.config.settings as S

    class _BadSettings:
        def __init__(self, *a, **k):
            raise ValueError("settings broken")

    monkeypatch.setattr(S, "Settings", _BadSettings)
    monkeypatch.delenv("HERO_DATA_MODE", raising=False)
    from hero_quant.data.loaders.tencent import TencentLoader
    from hero_quant.data.loaders.yahoo import YahooLoader

    # settings 失败 -> env 回退 live -> 后续按 live 走
    TencentLoader()._rate_limit()
    with pytest.raises(Exception):
        YahooLoader().get_bars("AAPL.US", "2024-01-01", "2024-01-02")
    import importlib
    importlib.reload(S)


def test_j20_yahoo_fields(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s
    importlib.reload(s)
    import types
    import unittest.mock as mock
    import pandas as pd
    from hero_quant.data.loaders.yahoo import DataValidationError, YahooLoader

    loader = YahooLoader()
    idx = pd.date_range("2024-01-01", periods=1, freq="D")
    # 缺字段
    df_nofield = pd.DataFrame({"Open": [1.0]}, index=idx)
    fy = types.SimpleNamespace(download=lambda *a, **k: df_nofield,
                               Ticker=lambda *a, **k: mock.MagicMock())
    with mock.patch.dict("sys.modules", {"yfinance": fy}):
        with pytest.raises(DataValidationError):
            loader.get_bars("AAPL.US", "2024-01-01", "2024-01-02")
    # 非法数值 + 负 volume
    df_bad = pd.DataFrame({"Open": ["bad"], "High": [1.0], "Low": [1.0],
                           "Close": [1.0], "Volume": [1]}, index=idx)
    fy2 = types.SimpleNamespace(download=lambda *a, **k: df_bad,
                                Ticker=lambda *a, **k: mock.MagicMock())
    with mock.patch.dict("sys.modules", {"yfinance": fy2}):
        with pytest.raises(DataValidationError):
            loader.get_bars("AAPL.US", "2024-01-01", "2024-01-02")
    df_neg = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                           "Close": [1.0], "Volume": [-5]}, index=idx)
    fy3 = types.SimpleNamespace(download=lambda *a, **k: df_neg,
                                Ticker=lambda *a, **k: mock.MagicMock())
    with mock.patch.dict("sys.modules", {"yfinance": fy3}):
        with pytest.raises(DataValidationError):
            loader.get_bars("AAPL.US", "2024-01-01", "2024-01-02")
    # 无 timeout 形参旧版 yfinance
    df_ok = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                          "Close": [1.0], "Volume": [1]}, index=idx)

    def _no_timeout(*a, **k):
        if "timeout" in k:
            raise TypeError("no timeout param")
        return df_ok
    fy4 = types.SimpleNamespace(download=_no_timeout,
                                Ticker=lambda *a, **k: mock.MagicMock())
    with mock.patch.dict("sys.modules", {"yfinance": fy4}):
        bars, _ = loader.get_bars("AAPL.US", "2024-01-01", "2024-01-02")
        assert len(bars) == 1


def test_j20_heartbeat_full():
    from hero_quant.telemetry import heartbeat as H

    H._set_emitter(lambda e: None)
    assert H._get_emitter() is not None
    H._set_emitter(None)
    H.temporal_heartbeat({"a": 1})
    assert H.get_temporal_heartbeat_details() is None or isinstance(H.get_temporal_heartbeat_details(), dict)
    assert H._probe_downstream_status() is not None
    H.mark_probe_downstream_down("j20")
    H.clear_probe_override()
    assert H.probe_temporal_sidecar() is not None
    assert isinstance(H.probe_temporal_sidecar_v2(), dict)
    assert isinstance(H.sidecar_heartbeat_probe(), dict)
    t = H.HeartbeatTimer(name="j20", interval=60)
    with t:
        assert t.probe() is not None
        t.heartbeat({"x": 1})
        assert t.get_heartbeat_details() is None or isinstance(t.get_heartbeat_details(), dict)
    t.stop()
    t2 = H.HeartbeatTimer(name="j20b", interval=0.1, sidecar_path=None, use_temporal=False)
    t2.stop()


def test_j20_limits_outer_and_ccxt_health(monkeypatch):
    import os
    import hero_quant.config.limits as L

    class _BadEnv:
        def get(self, *a, **k):
            raise RuntimeError("env broken")
    monkeypatch.setattr(os, "environ", _BadEnv())
    import importlib
    importlib.reload(L)
    assert L.TOOL_RESULT_LIMIT == 10_000
    monkeypatch.undo()
    importlib.reload(L)
    import unittest.mock as mock
    with mock.patch.dict("sys.modules", {"ccxt": mock.MagicMock()}):
        from hero_quant.data.loaders.ccxt_loader import CCXTLoader
        assert CCXTLoader().health()["ccxt_available"] is True
