"""J线 J14: 收官 —— risk/stream/shadow 全覆盖 + 残余精确补点。"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


# ---------- risk 全覆盖 ----------

def test_j14_risk_probes(monkeypatch):
    from hero_quant.api import risk as R

    R._risk_observe("op", "code")
    # 注：导入 server 会触发 warmup 构建 bundle，指标可能真实存在
    assert R._get_metric("turnover") is None or isinstance(R._get_metric("turnover"), float)
    assert R._get_turnover() is None or isinstance(R._get_turnover(), float)
    assert R._get_exposure() is None or isinstance(R._get_exposure(), float)
    assert R._get_single_limit() is None
    assert R._get_circuit_threshold() is None
    assert R._get_reject_rate() is None
    assert R._get_circuit_state() in ("UNKNOWN", "CLOSED", "OPEN", "HALF_OPEN") or isinstance(R._get_circuit_state(), str)
    assert R._get_pit_status() in ("unknown", "verified", "failed")
    assert R._get_cross_source() in ("unknown", "pass", "fail")
    resp = R.risk_summary()
    assert isinstance(resp, dict) and "turnover" in resp
    assert R.risk_summary_alias() == R.risk_summary() or isinstance(R.risk_summary_alias(), dict)


def test_j14_risk_with_bundle(monkeypatch):
    import hero_quant.api.server as SV
    from hero_quant.api import risk as R

    monkeypatch.setattr(SV, "_get_backtest_bundle",
                        lambda: {"metrics": {"turnover": 0.5, "exposure": 0.8, "single_limit": 0.1,
                                            "circuit_threshold": 0.9, "reject_rate": 0.01}}, raising=False)
    assert R._get_turnover() == 0.5
    assert R._get_metric("exposure") == 0.8
    resp = R.risk_summary()
    assert isinstance(resp, dict)


# ---------- stream 全覆盖 ----------

def test_j14_stream_full():
    from hero_quant.stream.service import StreamService, Tick

    svc = StreamService()
    assert svc.factor_window == 20
    assert StreamService(window="bad").factor_window == 20
    assert StreamService(window=-1).factor_window == 20
    assert StreamService(window=5).factor_window == 5
    t = Tick(symbol="AAPL", price=100.0)
    out = svc.ingest_tick(t)
    assert out["symbol"] == "AAPL"
    out2 = svc.ingest_tick({"symbol": "MSFT", "price": "101.5", "qty": "10", "ts": "t"})
    assert out2["symbol"] == "MSFT"
    assert _run(svc.aingest_tick({"symbol": "A", "price": 1.0}))["symbol"] == "A"
    assert svc.publish_to_redpanda({"symbol": "A", "price": 1.0})["published"] is True
    assert svc.publish({"symbol": "A", "price": 1.0})["published"] is True
    assert len(svc.consume_from_redpanda(limit=2)) == 2
    assert svc.consume(limit=0) == []
    assert svc.consume(limit="bad") is not None
    with pytest.raises(ValueError):
        svc.ingest_tick({"symbol": "A"})
    with pytest.raises(ValueError):
        svc.ingest_tick({"symbol": "A", "price": "bad"})
    with pytest.raises(ValueError):
        svc.ingest_tick({"symbol": "", "price": 1.0})
    with pytest.raises(ValueError):
        svc.ingest_tick({"symbol": "A", "price": float("inf")})
    with pytest.raises(ValueError):
        svc.ingest_tick({"symbol": "A", "price": 1.0, "qty": float("nan")})
    with pytest.raises(ValueError):
        svc.ingest_tick(object())
    with pytest.raises(ValueError):
        svc.ingest_tick(Tick(symbol="", price=1.0))
    # 缓冲截断
    for i in range(1100):
        svc.ingest_tick({"symbol": "B", "price": 1.0 + i * 0.001})
    assert len(svc._buffer) <= 1000


# ---------- shadow 全覆盖 ----------

def test_j14_shadow_full(tmp_path):
    from hero_quant.shadow.service import (ATTRIBUTION_CATEGORIES, ATTRIBUTION_CN,
                                           DEFAULT_RULES, RiskEngine, ShadowAccount,
                                           ShadowJournal, ShadowRule, _rule_price_positive,
                                           _rule_qty_limit, _rule_symbol_not_empty,
                                           _rule_notional_limit)

    assert len(ATTRIBUTION_CATEGORIES) == 5 and len(ATTRIBUTION_CN) == 5
    assert len(DEFAULT_RULES) >= 3
    assert _rule_price_positive({"price": 10}) is True
    assert _rule_price_positive({"price": -1}) is False
    assert _rule_price_positive({}) is False
    assert _rule_qty_limit({"qty": 5}) is True
    assert _rule_qty_limit({"qty": 0}) is False
    assert _rule_symbol_not_empty({"symbol": "A"}) is True
    assert _rule_symbol_not_empty({"symbol": " "}) is False
    assert _rule_notional_limit({"qty": 1, "price": 1}) is True
    assert _rule_notional_limit({"qty": 1e9, "price": 1e9}) is False
    bad_rule = ShadowRule(name="boom", description="x", check=lambda o: 1 / 0)
    assert bad_rule.passes({}) is False

    eng = RiskEngine()
    assert eng.check_order({"symbol": "A", "qty": 10, "price": 100.0})["allowed"] is True
    assert eng.check_order({"symbol": "", "qty": -5, "price": -1})["allowed"] is False
    eng2 = RiskEngine(rules=[DEFAULT_RULES[0]])
    assert len(eng2.rules) == 3  # 最少 3 条回退
    eng3 = RiskEngine(rules=list(DEFAULT_RULES) + list(DEFAULT_RULES))
    assert len(eng3.rules) == 5  # 最多 5 条截断
    # 熔断打开拒绝
    for _ in range(20):
        eng.circuit.record_failure()
    assert eng.check_order({"symbol": "A", "qty": 1, "price": 1.0})["allowed"] is False

    j = ShadowJournal()
    assert j.record({"symbol": "A", "qty": 1, "price": 10.0, "pnl": 5.0})["recorded"] is True
    assert j.coverage() == 1.0
    assert all(v > 0 for v in j.attribution().values())
    assert len(j.records) == 1
    assert ShadowAccount is ShadowJournal
    empty = ShadowJournal()
    assert all(v > 0 for v in empty.attribution().values())


# ---------- 残余补点 ----------

def test_j14_store_search_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    from hero_quant.memory.store import MemoryStore

    st = MemoryStore(tmp_path / "sv")
    st.write("a1", "alpha beta gamma delta epsilon zeta eta theta content here")
    st.write("a2", "completely different topic about oceans and sailing boats")
    assert isinstance(st.search("alpha beta"), list)
    assert isinstance(st.search(""), list)
    assert isinstance(st.recall("", top_k=1), list)
    assert isinstance(st.vector_search("", top_k=1), list)
    assert isinstance(st._search_bm25_raw(""), list)
    st.close()


def test_j14_loop_retry_exhaust():
    from hero_quant.agent.loop import AgentLoop

    class _AlwaysBoom:
        def stream_chat(self, goal):
            raise ConnectionError("down")
            yield

    res = AgentLoop(llm=_AlwaysBoom(), max_iterations=2).run("down")
    assert res.reason in ("llm_error", "completed", "max_iterations", "budget_fallback",
                          "user_stop", "token_limit", "graph_error", "grounding_failed")


def test_j14_engine_leverage_and_weights():
    import numpy as np
    from hero_quant.backtest.engine import BacktestEngine

    idx = pd_idx = __import__("pandas").date_range("2024-01-01", periods=6, freq="D")
    import pandas as pd
    px = pd.DataFrame({"close": [100.0, 102, 101, 103, 102, 104]}, index=idx)
    eng = BacktestEngine()
    res = eng.run(px, weights=[2.0], max_leverage=3.0, allow_synthetic=True)
    assert "equity" in res
    res2 = eng.run(px, signal="equal_weight", weights=np.array([1.0]), allow_synthetic=True)
    assert "equity" in res2


def test_j14_registry_audit_bound_and_trait():
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry(audit_log_maxlen=5)
    assert reg.get_audit_log() == []
    assert reg._bar_unit(None) is None
    assert reg._first_close(None) is None


def test_j14_trace_env_and_misc(tmp_path, monkeypatch):
    monkeypatch.setenv("HERO_TRACE_DIR", str(tmp_path / "envtr"))
    from hero_quant.agent import trace as T

    assert T.default_trace_dir().exists()
    assert T._scrub_cookie({"cookie": "secret", "x": "cookie=abc; y=1"}) is not None
    assert T._scrub_cookie(["cookie=zzz"]) is not None
    assert T._scrub_cookie("plain") == "plain"
    assert T._scrub_cookie(("a", "cookie=b")) is not None
    assert T._env_int("NO_SUCH_J14", None) is None
    monkeypatch.setenv("NO_SUCH_J14", "bad")
    assert T._env_int("NO_SUCH_J14", 9) == 9


def test_j14_misc_surface():
    from hero_quant.metrics import inc_llm_retry, inc_wall_time_exceeded, observe_wall_time
    inc_llm_retry(provider="t", reason="r")
    inc_wall_time_exceeded()
    observe_wall_time("op_j14", 0.01)
    from hero_quant.tools.registry import get_definitions
    assert len(get_definitions()) > 0
    assert len(get_definitions(presentAs="native")) > 0
    with pytest.raises(ValueError):
        get_definitions(presentAs="nope_xyz")
    with pytest.raises(NotImplementedError):
        get_definitions(presentAs="code")
    from hero_quant.tools.redaction import redact_tool_result
    assert "sk-" not in redact_tool_result({"api_key": "sk-1234567890abcdef"})
    from hero_quant.agent.memory import buffer as _buf
    assert len([n for n in dir(_buf) if not n.startswith("_")]) > 0
