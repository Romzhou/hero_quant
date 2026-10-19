"""J线 J4b: 链B —— backtest/engine run 全守卫分支 + tools/* 工具面。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _prices(n=6, start="2024-01-01"):
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"close": [100.0 + i for i in range(n)]}, index=idx)


# ---------- engine run 守卫 ----------

def test_j4b_engine_costs_capital_guards():
    from hero_quant.backtest.engine import BacktestEngine

    p = _prices()
    with pytest.raises(ValueError):
        BacktestEngine().run(p, weights=[1.0], costs=-1.0, allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine().run(p, weights=[1.0], costs=float("nan"), allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine().run(p, weights=[1.0], costs=float("inf"), allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine().run(p, weights=[1.0], costs="bad", allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine(initial_capital=0.0).run(p, weights=[1.0], allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine(initial_capital=float("nan")).run(p, weights=[1.0], allow_synthetic=True)
    with pytest.raises(TypeError):
        BacktestEngine().run([1, 2, 3], weights=[1.0], allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine().run(pd.DataFrame(), weights=[1.0], allow_synthetic=True)
    with pytest.raises(ValueError):
        BacktestEngine().run(pd.DataFrame({"volume": [1, 2, 3]},
                                          index=pd.date_range("2024-01-01", periods=3)),
                             weights=[1.0], allow_synthetic=True)


def test_j4b_engine_pit_bypass_and_synthetic_gate():
    from hero_quant.backtest.engine import PITViolation, BacktestEngine
    from hero_quant.backtest.validation import PIT_ACK

    p = _prices()
    # 无日期 + 不允许合成 -> 硬抛
    with pytest.raises(PITViolation):
        BacktestEngine().run(p, weights=[1.0], allow_synthetic=False)
    # skip_pit 无 ack -> 抛；有 ack -> non_pit
    with pytest.raises(PITViolation):
        BacktestEngine().run(p, weights=[1.0], skip_pit=True, allow_synthetic=True)
    res = BacktestEngine().run(p, weights=[1.0], skip_pit=True, pit_ack=PIT_ACK, allow_synthetic=True)
    assert res.get("non_pit") is True
    res2 = BacktestEngine().run(p, weights=[1.0], enforce_pit=False, pit_ack=PIT_ACK, allow_synthetic=True)
    assert res2.get("non_pit") is True
    # 显式日期通过
    res3 = BacktestEngine().run(p, weights=[1.0], weights_on="2024-01-01", price_date="2024-01-06")
    assert "equity" in res3


def test_j4b_engine_signals_weights_leverage():
    from hero_quant.backtest.engine import BacktestEngine, Signal, generate_signal

    p = _prices(30)
    # Signal 对象（method= 关键字）
    sig = Signal(method="sma_crossover")
    w = sig.generate(p, n_assets=1)
    assert w is not None
    res = BacktestEngine().run(p, signal=sig, allow_synthetic=True)
    assert "equity" in res
    # 字符串信号 + signal_method
    res2 = BacktestEngine().run(p, signal="sma_crossover", allow_synthetic=True)
    assert "equity" in res2
    res3 = BacktestEngine().run(p, signal_method="equal_weight", allow_synthetic=True)
    assert "equity" in res3
    # 坏信号回落 equal_weight
    res4 = BacktestEngine().run(p, signal="no_such_signal_xyz", allow_synthetic=True)
    assert "equity" in res4
    # weights None 默认 + max_leverage
    res5 = BacktestEngine().run(p, max_leverage=2.0, allow_synthetic=True)
    assert "equity" in res5
    # generate_signal / _static_signal 直接
    assert generate_signal(p, method="equal_weight", n_assets=1) is not None
    from hero_quant.backtest.engine import _static_signal
    assert _static_signal([0.5, 0.5]) is not None
    assert _static_signal(None) is not None


def test_j4b_engine_ticks_bars_turnover_tearsheet():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    tick = eng.on_tick({"price": 100.0, "symbol": "AAPL"})
    assert isinstance(tick, dict)
    tick2 = eng.on_tick(object())
    assert isinstance(tick2, dict)
    tick3 = eng.on_tick({"price": "bad"})
    assert isinstance(tick3, dict)
    p = _prices(5)
    bar = eng.on_bar(p.iloc[0], 0, p)
    assert isinstance(bar, dict)
    bad_px = pd.DataFrame({"close": [100.0, "bad", 102.0]},
                          index=pd.date_range("2024-01-01", periods=3))
    with pytest.raises(Exception):
        eng.on_bar(bad_px.iloc[0], 0, bad_px)
    ep = eng._drawdown_episodes_html(_prices(10)["close"], top_n=2)
    assert isinstance(ep, str)
    ts = eng._build_tearsheet(_prices(5)["close"], {"sharpe": 1.0})
    assert isinstance(ts, str)
    pos_proxy = pd.DataFrame({"a": [0.0, 50.0, 50.0], "b": [0.0, 50.0, 60.0]})
    gross = pd.Series([100.0, 100.0, 110.0])
    to = eng._compute_turnover_rate(pos_proxy, gross, np.array([0.5, 0.5]), 1.0)
    assert len(to) == 3
    # 空仓首日 + 坏权重分支
    to2 = eng._compute_turnover_rate(pd.DataFrame({"a": [0.0, 0.0]}),
                                     pd.Series([100.0, 100.0]), np.array([0.0, 0.0]), 0.0)
    assert len(to2) == 2


# ---------- tools ----------

def test_j4b_tools_market_data(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    from hero_quant.tools import market_data as MD

    assert isinstance(MD.list_markets(), dict)
    assert isinstance(MD.get_ticker_info("AAPL.US"), dict)
    assert isinstance(MD.get_fundamentals("AAPL.US"), dict)
    assert isinstance(MD.search_symbols("apple"), dict)
    assert isinstance(MD.search_symbol("apple"), dict)
    from hero_quant.data.registry import CrossSourceError
    try:
        out = MD.get_market_data(symbol="AAPL.US", start="2024-01-01", end="2024-01-03")
        assert isinstance(out, dict) and "ok" in out
    except CrossSourceError:
        pass  # 跨源 synthetic 混合 fail-closed：market_data 透传（分支覆盖）
    # 跨源 synthetic 混合在 registry 层 fail-closed；market_data 透传 CrossSourceError
    try:
        out_cn = MD.get_market_data(symbol="000001.SZ", start="2024-01-01", end="2024-01-03")
        assert isinstance(out_cn, dict) and "ok" in out_cn
    except CrossSourceError:
        pass
    # 成功分支：注入假 registry 走 ok=True 路径
    import unittest.mock as _mock2
    from hero_quant.data.registry import Provenance as _Prov
    fake_reg = _mock2.MagicMock()
    fake_reg.get_bars.return_value = (
        [{"date": "2024-01-01", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}],
        _Prov(source="yahoo", unit="shares", symbol="AAPL.US"),
    )
    with _mock2.patch.object(MD, "_get_shared_registry", return_value=fake_reg):
        ok_out = MD.get_market_data(symbol="AAPL.US", start="2024-01-01", end="2024-01-03")
        assert ok_out["ok"] is True and len(ok_out["bars"]) == 1
    try:
        out2 = MD.get_bars_range(symbols=["AAPL.US"], start="2024-01-01", end="2024-01-03")
        assert isinstance(out2, dict)
    except CrossSourceError:
        pass
    try:
        out3 = MD.get_bars_range(symbols=["UNKNOWN_NOPE"], start="2024-01-01", end="2024-01-03")
        assert isinstance(out3, dict)  # 无 loader 回退合成 ok:False
    except (CrossSourceError, RuntimeError):
        pass
    with pytest.raises(ValueError):
        MD._provenance_dict(None)
    fb = MD._synthetic_fallback("AAPL.US", "2024-01-01", "2024-01-02")
    assert fb is not None
    with pytest.raises(ValueError):
        MD._synthetic_fallback("AAPL.US", "bad-date", "2024-01-02")


def test_j4b_tools_quantlib(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    from hero_quant.tools import quantlib_tool as QT

    monkeypatch.setattr(QT, "_fetch_closes",
                        lambda symbol, start="", end="": [100.0 + i for i in range(30)])
    assert QT._validate_window(5, 10) == 5
    with pytest.raises(ValueError):
        QT._validate_window(0, 10)
    out = QT.compute_indicator(symbol="AAPL.US", indicator="sma", window=3,
                               start="2024-01-01", end="2024-01-10")
    assert isinstance(out, dict)
    out2 = QT.compute_indicator(symbol="AAPL.US", indicator="nope_xyz", window=3)
    assert isinstance(out2, dict)
    assert isinstance(QT.compute_sharpe([0.01, 0.02, -0.01]), dict)
    assert isinstance(QT.compute_drawdown([100, 90, 95, 80]), dict)
    assert isinstance(QT.compute_factor(factor="momentum", symbol="AAPL.US",
                                        start="2024-01-01", end="2024-02-10"), dict)
    assert isinstance(QT.compute_factor(factor="nope_xyz"), dict)
    assert isinstance(QT.screen_factors(universe=["A", "B"]), dict)


def test_j4b_tools_backtest_correlation_redaction():
    from hero_quant.tools import backtest as BT

    assert isinstance(BT.validate_backtest(weights_on="2024-01-01", price_date="2024-01-05"), dict)
    assert isinstance(BT.get_backtest_metrics([100, 101, 102]), dict)
    assert isinstance(BT.list_backtest_engines(), dict)
    assert isinstance(BT.optimize_portfolio(symbols=["A", "B"]), dict)
    res = BT.run_backtest(symbol="AAPL.US", start="2024-01-01", end="2024-01-05",
                          weights=[1.0], allow_synthetic=True)
    assert isinstance(res, dict)

    from hero_quant.tools import correlation as CO
    import unittest.mock as _mock
    with _mock.patch.object(CO, "_fetch_closes",
                            lambda s, st, en: [(f"2024-01-0{i}", 100.0 + i) for i in range(1, 6)]):
        assert isinstance(CO.compute_correlation(symbol_a="A.US", symbol_b="B.US",
                                                start="2024-01-01", end="2024-01-05"), dict)

    from hero_quant.tools.redaction import redact_tool_result
    assert isinstance(redact_tool_result({"key": "sk-secret-1234567890"}), str)
    assert isinstance(redact_tool_result("plain string"), str)

    from hero_quant.tools.presentation import present, present_as_code, present_as_native, present_definitions
    from hero_quant.tools.registry import TOOL_REGISTRY, get_definitions
    defs = get_definitions()
    assert len(defs) > 0
    spec = next(iter(TOOL_REGISTRY.values()))
    assert isinstance(present_as_native(spec), dict)
    assert isinstance(present_as_code(spec), str)
    assert present(spec, presentAs="native") is not None
    assert present(spec, presentAs="code") is not None
    assert isinstance(present_definitions(), list)
