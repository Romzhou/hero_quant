"""PR-214 Risk 归因 / Shadow 真数据 — 恒等式、降级分支、真实拒单、端点与源码断言。

只跑定向用例；不启动真实 Redis（fakeredis / 进程内 TestClient）。
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _mk_bundle(pos=None, prices=None, metrics=None, bars=None, repro=None):
    """构造与 server._get_backtest_bundle() 同构的产物（prices 为 [{"date","close"}]）。"""
    idx = pd.to_datetime(["2024-03-0%d" % d for d in range(1, 7)])
    if pos is None:
        pos = pd.DataFrame({"600519.SH": [100.0, 120.0, 80.0, 90.0, 110.0, 130.0]}, index=idx)
    if prices is None:
        prices = [{"date": d.date().isoformat(), "close": c} for d, c in zip(idx, [10.0, 11.0, 10.5, 12.0, 11.5, 12.5])]
    return {
        "metrics": metrics if metrics is not None else {"cumulative_return": 0.21, "max_drawdown": 0.05},
        "positions": pos,
        "prices": prices,
        "csv": "",
        "sample": {"bars": bars if bars is not None else len(pos.index)},
        "reproducibility": repro if repro is not None else {
            "config_hash": "cfg-1",
            "data_fingerprint": "fp-1",
            "result_hash": "rh-1",
            "code_commit": "abc123",
            "hash_version": "v1",
            "reproducible": True,
        },
    }


# ---------------------------------------------------------------- 恒等式 / 口径


def test_identity_holds_single_symbol():
    from hero_quant.risk.attribution import ATTRIBUTION_KEYS, compute_attribution, default_cost_rate

    r = compute_attribution(_mk_bundle())
    comp = r["components"]
    assert set(comp) == set(ATTRIBUTION_KEYS), comp
    # 单标的无横截面 → 选股类不可算（null，不是 0.01 占位）
    assert comp["selection"] is None
    assert "selection_single_symbol" in r["not_applicable"]
    # cost 用引擎真实默认成本率（不硬编码 0.0005）
    assert default_cost_rate() == pytest.approx(0.0005)
    assert comp["cost"] is not None and comp["cost"] < 0
    # risk_control = -|max_drawdown|，不进恒等式
    assert comp["risk_control"] == pytest.approx(-0.05)
    assert r["included_in_identity"]["risk_control"] is False
    # 恒等式：可算项 + residual == net_return（1e-9）
    summed = sum(v for k, v in comp.items() if k in ("timing", "selection", "cost") and v is not None)
    assert summed + comp["residual"] == pytest.approx(r["net_return"], abs=1e-9)
    assert r["coverage"] == pytest.approx(4 / 5)
    assert r["degraded"] is True and r["reason"] == "selection_single_symbol"
    assert r["reproducibility"]["data_fingerprint"] == "fp-1"
    assert r["metrics_cumulative_return"] == pytest.approx(0.21)  # 原样透传，不强行对齐
    assert r["turnover_notional"] == pytest.approx(210.0)  # |Δh| 之和：100+20+40+10+20+20


def test_multi_symbol_selection_is_real_and_identity_holds():
    from hero_quant.risk.attribution import compute_attribution

    idx = pd.to_datetime(["2024-03-0%d" % d for d in range(1, 7)])
    pos = pd.DataFrame(
        {"A": [50.0, 60.0, 40.0, 45.0, 55.0, 65.0], "B": [50.0, 60.0, 40.0, 45.0, 55.0, 65.0]},
        index=idx,
    )
    r = compute_attribution(_mk_bundle(pos=pos))
    comp = r["components"]
    # 2 列 → 选股类可算；单一价格序列广播后横截面无离散 → 真值 0.0（并给出可追溯 warning）
    assert comp["selection"] is not None
    assert any("cross_section_degenerate" in w for w in r["warnings"]), r["warnings"]
    summed = sum(v for k, v in comp.items() if k in ("timing", "selection", "cost") and v is not None)
    assert summed + comp["residual"] == pytest.approx(r["net_return"], abs=1e-9)
    assert r["coverage"] == pytest.approx(1.0)
    assert r["degraded"] is False and r["reason"] is None


def test_all_floats_finite():
    import math

    from hero_quant.risk.attribution import ATTRIBUTION_KEYS, compute_attribution

    r = compute_attribution(_mk_bundle())
    for k in ATTRIBUTION_KEYS:
        v = r["components"][k]
        assert v is None or math.isfinite(v), (k, v)
    for k in ("net_return", "coverage", "turnover_notional"):
        v = r[k]
        assert v is None or math.isfinite(v), (k, v)


def test_cost_rate_unavailable_is_null_not_zero(monkeypatch):
    """拿不到成本率 → cost 为 null + cost_rate_unavailable，绝不当 0 计入。"""
    import hero_quant.risk.attribution as attr

    monkeypatch.setattr(attr, "default_cost_rate", lambda: None)
    r = attr.compute_attribution(_mk_bundle())
    assert r["components"]["cost"] is None
    assert "cost_rate_unavailable" in r["not_applicable"]
    summed = sum(v for k, v in r["components"].items() if k in ("timing", "selection", "cost") and v is not None)
    assert summed + r["components"]["residual"] == pytest.approx(r["net_return"], abs=1e-9)


def test_max_drawdown_missing_is_null():
    from hero_quant.risk.attribution import compute_attribution

    r = compute_attribution(_mk_bundle(metrics={"cumulative_return": 0.1}))
    assert r["components"]["risk_control"] is None
    assert "max_drawdown_missing" in r["not_applicable"]


# ---------------------------------------------------------------- 降级分支


@pytest.mark.parametrize(
    "bundle,reason",
    [
        ({}, "positions_missing"),
        ({"metrics": {"turnover": 0.4}}, "positions_missing"),
        ({"positions": None, "csv": "", "sample": {"bars": 0}}, "no_market_data"),
    ],
)
def test_degraded_branches(bundle, reason):
    from hero_quant.risk.attribution import ATTRIBUTION_KEYS, compute_attribution

    r = compute_attribution(bundle)
    assert r["degraded"] is True
    assert r["reason"] == reason, r
    assert all(r["components"][k] is None for k in ATTRIBUTION_KEYS), r["components"]
    assert r["coverage"] == 0.0
    assert r["net_return"] is None


def test_prices_missing_and_insufficient_bars():
    from hero_quant.risk.attribution import compute_attribution

    no_prices = _mk_bundle(prices=None)
    no_prices["prices"] = None
    assert compute_attribution(no_prices)["reason"] == "prices_missing"

    short = _mk_bundle(
        pos=pd.DataFrame({"600519.SH": [1.0, 2.0]}, index=pd.to_datetime(["2024-03-01", "2024-03-02"])),
        prices=[{"date": "2024-03-01", "close": 10.0}, {"date": "2024-03-02", "close": 11.0}],
    )
    r = compute_attribution(short)
    assert r["degraded"] is True and r["reason"] == "insufficient_bars"
    assert r["components"]["timing"] is None


def test_static_bundle_shell_degrades(monkeypatch):
    """server._static_backtest_bundle() 空壳：sample.bars=0 → no_market_data。"""
    from hero_quant.api.server import _static_backtest_bundle
    from hero_quant.risk.attribution import compute_attribution

    r = compute_attribution(_static_backtest_bundle())
    assert r["degraded"] is True and r["reason"] == "no_market_data"
    assert r["components"]["residual"] is None
    assert r["reproducibility"] is not None  # 指纹原样透传，不补算


# ---------------------------------------------------------------- Shadow 重放


def test_shadow_real_reject_rate_gt_zero():
    """真实超大名义持仓 → 命中真实规则（qty_limit / notional_limit），拒单率 > 0。"""
    from hero_quant.risk.attribution import build_shadow_replay

    idx = pd.to_datetime(["2024-03-0%d" % d for d in range(1, 6)])
    pos = pd.DataFrame({"600519.SH": [2.0e7, -2.0e7, 6.0e6, -6.0e6, 1.0e5]}, index=idx)
    r = build_shadow_replay(
        _mk_bundle(pos=pos, prices=[{"date": d.date().isoformat(), "close": 10.0} for d in idx])
    )
    assert r["degraded"] is False, r
    assert r["orders_total"] == 5
    assert r["reject_rate"] is not None and r["reject_rate"] > 0
    assert r["allowed"] + r["rejected"] == r["orders_total"]
    assert r["rejected_by_rule"], r["rejected_by_rule"]
    assert set(r["rejected_by_rule"]) <= {"qty_limit", "notional_limit", "symbol_valid", "price_positive", "circuit"}
    assert r["notional_total"] == pytest.approx(1.041e8)
    assert r["turnover_notional"] == pytest.approx(1.041e8)
    assert r["turnover_mean_exposure"] == pytest.approx(1.041e8 / 1.042e7)


def test_shadow_uses_risk_engine_real_rules(monkeypatch):
    """必须真调 RiskEngine.check_order（不是 ShadowJournal.attribution 的 0.01 占位）。"""
    from hero_quant.risk import attribution as attr
    from hero_quant.shadow.service import RiskEngine

    calls = []

    def _spy_check(self, order):
        calls.append(dict(order))
        return {"allowed": True, "reason": "pass"}

    monkeypatch.setattr(RiskEngine, "check_order", _spy_check)
    r = attr.build_shadow_replay(_mk_bundle())
    assert len(calls) == r["orders_total"] >= 5
    assert all("symbol" in c and "qty" in c and "price" in c and "side" in c for c in calls)
    assert r["reject_rate"] == 0.0  # 全部放行时 0.0 是真实值，不是占位
    assert r["degraded"] is False


def test_shadow_no_fills_is_null_not_zero():
    from hero_quant.risk.attribution import build_shadow_replay

    r = build_shadow_replay({})
    assert r["degraded"] is True and r["reason"] == "no_fills"
    assert r["reject_rate"] is None, "无成交时拒单率必须 null，0.0 会冒充零拒单"
    assert r["orders_total"] is None and r["notional_total"] is None

    flat = _mk_bundle(pos=pd.DataFrame({"600519.SH": [0.0] * 6}, index=pd.to_datetime(["2024-03-0%d" % d for d in range(1, 7)])))
    r2 = build_shadow_replay(flat)
    assert r2["degraded"] is True and r2["reason"] == "no_fills"
    assert r2["reject_rate"] is None


# ---------------------------------------------------------------- 端点


def _client(monkeypatch, bundle):
    from fastapi.testclient import TestClient

    import hero_quant.api.server as srv

    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda *a, **k: bundle)
    return TestClient(srv.app)


def test_endpoints_real_bundle(monkeypatch):
    c = _client(monkeypatch, _mk_bundle())
    ra = c.get("/v1/risk/attribution")
    assert ra.status_code == 200, ra.text
    body = ra.json()
    assert body["coverage"] == pytest.approx(0.8)
    assert body["components"]["selection"] is None
    assert body["bars"] >= 3
    assert body["reproducibility"]["config_hash"] == "cfg-1"

    rs = c.get("/v1/risk/shadow")
    assert rs.status_code == 200, rs.text
    sbody = rs.json()
    assert sbody["degraded"] is False
    assert sbody["reject_rate"] == pytest.approx(0.0)
    assert sbody["orders_total"] == 6

    # 双注册惯例：/risk/... 与 /v1/risk/... 同源
    assert c.get("/risk/attribution").json()["coverage"] == pytest.approx(0.8)
    assert c.get("/risk/shadow").json()["orders_total"] == 6


def test_endpoints_degraded(monkeypatch):
    c = _client(monkeypatch, {})
    ra = c.get("/v1/risk/attribution").json()
    assert ra["degraded"] is True and ra["reason"] == "positions_missing"
    assert all(v is None for v in ra["components"].values())
    rs = c.get("/v1/risk/shadow").json()
    assert rs["degraded"] is True and rs["reason"] == "no_fills"
    assert rs["reject_rate"] is None


def test_summary_falls_back_to_real_positions(monkeypatch):
    """metrics 缺键时 exposure/single_limit/reject_rate 取真实计算值，不再恒 None。"""
    from hero_quant.api.risk import risk_summary

    import hero_quant.api.server as srv

    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda *a, **k: _mk_bundle(metrics={}))
    r = risk_summary()
    assert r["exposure"] is not None and r["exposure"] > 0, r
    assert r["single_limit"] is not None and 0 < r["single_limit"] <= 1.0, r
    assert r["reject_rate"] is not None, r
    assert "exposure_missing" not in (r.get("reason") or "")
    assert "single_limit_missing" not in (r.get("reason") or "")
    assert "reject_rate_missing" not in (r.get("reason") or "")


def test_summary_metrics_passthrough_wins(monkeypatch):
    """既有断言兼容：metrics 有值时原值透传，不被真实计算值覆盖。"""
    from hero_quant.api.risk import risk_summary

    import hero_quant.api.server as srv

    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda *a, **k: _mk_bundle(
        metrics={"exposure": 0.33, "turnover": 0.5, "single_limit": 0.1, "circuit_threshold": 0.2, "reject_rate": 0.01}
    ))
    r = risk_summary()
    assert r["exposure"] == pytest.approx(0.33)
    assert r["single_limit"] == pytest.approx(0.1)
    assert r["reject_rate"] == pytest.approx(0.01)
    assert r["circuit_threshold"] == pytest.approx(0.2)


# ---------------------------------------------------------------- 源码断言


HARDCODED = [
    "0.62",
    "+1.2%",
    "+0.8%",
    "-0.1%",
    "-0.4%",
    "+0.2%",
    "\"0.42\"",
    "1.2x",
    "600519 18%",
]


def test_risk_tsx_has_no_hardcoded_numbers():
    src = (REPO / "frontend/src/pages/Risk.tsx").read_text(encoding="utf-8")
    for token in HARDCODED:
        assert token not in src, f"Risk.tsx 仍写死 {token}"
    assert "NaN%" not in src
    for endpoint in ("/v1/risk/attribution", "/v1/risk/shadow"):
        assert endpoint in src, f"Risk.tsx 未接 {endpoint}"


def test_risk_py_has_no_hardcoded_numbers():
    src = (REPO / "src/hero_quant/api/risk.py").read_text(encoding="utf-8")
    for token in ("0.62", "0.42", "1.2x", "0.0005"):
        assert token not in src, f"risk.py 仍写死 {token}"


def test_server_bundle_carries_prices():
    """server 改动：bundle 与 L2 缓存 payload 必须带 prices，否则归因无从计算。"""
    src = (REPO / "src/hero_quant/api/server.py").read_text(encoding="utf-8")
    assert "prices" in src
    assert "_backtest_prices_payload" in src

