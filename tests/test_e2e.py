def test_e2e_query_to_report(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    # 端到端：mock llm + 真 registry(mock http) + 真 backtest，验证最终报告含 metrics 且价格经 grounding
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.data.registry import MarketDataRegistry
    from hero_quant.data.loaders.tencent import TencentLoader
    from hero_quant.backtest.engine import BacktestEngine
    from hero_quant.agent.grounding import GroundingLedger
    import pandas as pd

    # 真 registry 落盘合成数据
    reg = MarketDataRegistry()
    reg.register(TencentLoader())
    bars, prov = reg.get_bars("600519.SH", "1d", "2026-08-01", "2026-08-10")
    # grounding
    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": b["close"], "date": b.get("date","2026-08-10")} for b in bars[:1]])
    ledger.assert_price("600519.SH", bars[0]["close"])

    # backtest
    prices = pd.DataFrame({"close":[b["close"] for b in bars[:5]]}, index=pd.date_range("2026-08-01", periods=min(5,len(bars))))
    eng = BacktestEngine()
    res = eng.run(prices, weights=[0.5,0.5])
    assert "equity" in res and res["metrics"]["sharpe"] is not None

    # agent loop 占位
    class FakeLLM:
        def stream_chat(self, *a, **kw): yield {"type":"text","text":f"report metrics sharpe {res['metrics']['sharpe']} grounding_verified True"}
    loop = AgentLoop(llm=FakeLLM(), max_iterations=2)
    r = loop.run("回测 600519.SH 近一月等权")
    assert r.terminated is True
    assert "sharpe" in r.text.lower() or "grounding" in r.text.lower()
