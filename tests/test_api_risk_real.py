"""Test risk real data — Task15 Wave3 Top4 TDD."""

def test_risk_no_hardcode():
    from hero_quant.api.risk import risk_summary

    r = risk_summary()
    # 修复后不应恒返回 0.62 且无 degraded，应返回真实值或 None 并标记 degraded
    assert r["exposure"] != 0.62 or r.get("degraded") is True


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
