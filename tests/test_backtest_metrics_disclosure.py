"""R8-1b：回测指标口径披露 —— sample / sharpe_basis / warnings 三态。

锁定的响应契约（docs/plan-2026-10-28-round8.md §4 R8-1b）：
- bars < 60：metrics.sharpe 置 null（保留 key），warnings 含 insufficient_sample，sharpe_basis 为 null；
- bars >= 60：sharpe 为数值，sharpe_basis == "annualized_252"；
- 无行情：sample == {"bars": 0, "start": null, "end": null}，warnings 含 no_market_data，不编造日期。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _prices(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05", periods=n, freq="B")
    vals = 100.0 + np.cumsum(np.sin(np.arange(n)) * 0.5 + 0.05)
    df = pd.DataFrame({"close": vals}, index=idx)
    df.index.name = "date"
    return df


@pytest.fixture()
def client(monkeypatch):
    """隔离两级缓存，保证每个用例都真算一遍。"""
    import hero_quant.api.server as srv
    from fastapi.testclient import TestClient

    monkeypatch.setattr(srv, "_read_backtest_bundle_cache", lambda tenant=None, bars=None: None)
    monkeypatch.setattr(srv, "_write_backtest_bundle_cache", lambda bundle, tenant=None, bars=None: None)
    monkeypatch.setattr(srv, "_backtest_cache", {}, raising=False)
    return TestClient(srv.app)


def _metrics_body(client, monkeypatch, prices):
    import hero_quant.api.server as srv

    # PR-125：loader 新增内部窗口参数（target），monkeypatch 用 *a/**k 吃掉，不影响本文件断言。
    monkeypatch.setattr(srv, "_load_backtest_prices", lambda pd_mod, *a, **k: prices, raising=False)
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: False, raising=False)
    r = client.get("/v1/backtest/metrics.json")
    assert r.status_code == 200, r.text[:300]
    return r.json()


def test_short_sample_suppresses_annualized_sharpe(client, monkeypatch):
    """20 根 K 线不足以年化：sharpe 必须为 null 且显式披露样本不足。"""
    prices = _prices(20)
    body = _metrics_body(client, monkeypatch, prices)

    assert body["sample"] == {
        "bars": 20,
        "start": prices.index[0].date().isoformat(),
        "end": prices.index[-1].date().isoformat(),
    }
    assert "sharpe" in body, "sharpe key 必须保留，保持 schema 稳定"
    assert body["sharpe"] is None
    assert body["sharpe_basis"] is None
    assert "insufficient_sample" in body["warnings"]
    # 非年化指标照旧透出
    for key in ("cumulative_return", "volatility", "max_drawdown"):
        assert key in body, f"{key} 不应因样本不足被删"


def test_long_sample_keeps_annualized_sharpe(client, monkeypatch):
    """>=60 根 K 线可以年化：sharpe 为数值，basis 标明口径。"""
    body = _metrics_body(client, monkeypatch, _prices(120))

    assert body["sample"]["bars"] == 120
    assert isinstance(body["sharpe"], (int, float)) and body["sharpe"] is not None
    assert body["sharpe_basis"] == "annualized_252"
    assert "insufficient_sample" not in body["warnings"]


def test_no_market_data_discloses_zero_sample(client, monkeypatch):
    """拿不到真行情：零样本 + no_market_data，不得编造日期或指标。"""
    body = _metrics_body(client, monkeypatch, None)

    assert body["sample"] == {"bars": 0, "start": None, "end": None}
    assert "no_market_data" in body["warnings"]
    assert body["sharpe_basis"] is None


def test_demo_mode_uses_same_disclosure_rule(client, monkeypatch):
    """HERO_BACKTEST_DEMO=1 只是换数据源，口径规则与标记同步生效。

    PR-125：demo 根数跟随默认窗口（20 → 60）。60 根恰好达到年化门槛，所以这里断的是
    "同一套规则">= 60 走年化、标记仍为 synthetic/degraded；<60 仍置 null 的用例见
    tests/test_pr125_backtest_bars.py（demo 20 根）。
    """
    import hero_quant.api.server as srv

    monkeypatch.setattr(srv, "_load_backtest_prices", lambda pd_mod, *a, **k: None, raising=False)
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: True, raising=False)
    r = client.get("/v1/backtest/metrics.json")
    assert r.status_code == 200, r.text[:300]
    body = r.json()

    assert body["sample"]["bars"] == srv._BACKTEST_BARS_TARGET
    assert isinstance(body["sharpe"], (int, float)) and body["sharpe"] is not None
    assert body["sharpe_basis"] == "annualized_252"
    assert "insufficient_sample" not in body["warnings"]
    assert body["synthetic"] is True
    assert body["degraded"] is True
