"""Test risk real data — Task15 Wave3 Top4 行为断言（不再用 “!=0.62 or degraded” 恒真兜底）。"""
import pytest


def test_risk_no_hardcode(monkeypatch):
    """metrics 缺失 -> exposure 必须 None 且 degraded+reason；metrics 存在 -> 原值透传。"""
    import hero_quant.api.server as srv
    from hero_quant.api.risk import risk_summary

    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda: {})
    r = risk_summary()
    assert r["exposure"] is None, f"缺 metrics 时不得再返回硬编码 0.62，got {r['exposure']}"
    assert r.get("degraded") is True
    assert "exposure_missing" in (r.get("reason") or "")

    metrics = {
        "exposure": 0.33,
        "turnover": 0.5,
        "single_limit": 0.1,
        "circuit_threshold": 0.2,
        "reject_rate": 0.01,
    }
    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda: {"metrics": metrics})
    r2 = risk_summary()
    assert r2["exposure"] == pytest.approx(0.33), "真实 exposure 必须原值透传，不得替换成 0.62"


def test_turnover_none_degraded(monkeypatch):
    import hero_quant.api.server as srv
    from hero_quant.api.risk import _get_turnover

    # bundle 缺失 turnover 时应返回 None 而非 0.42
    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda: {})
    assert _get_turnover() is None, "missing turnover should return None not 0.42"

    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda: {"metrics": {}})
    assert _get_turnover() is None

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_get_backtest_bundle", _boom)
    assert _get_turnover() is None, "exception should return None not 0.42"
