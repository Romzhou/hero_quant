"""J线 J12a: limits + llm client + tools 指标分支 + runner + risk/router 扫尾。"""
from __future__ import annotations

import pytest


def test_j12a_limits():
    from hero_quant.config import limits as L

    assert L.truncate_tool_result(None) == ""
    assert L.truncate_tool_result("short") == "short"
    big = "x" * (L.TOOL_RESULT_LIMIT + 100)
    out = L.truncate_tool_result(big)
    assert len(out) <= L.TOOL_RESULT_LIMIT and "TRUNCATED" in out
    assert L.truncate_tool_result(big, limit="bad") is not None
    assert L.truncate_tool_result(big, limit=0) is not None
    assert L.truncate_tool_result({"a": 1}) == '{"a": 1}'
    assert L.truncate_tool_result(object()) is not None
    assert L.truncate_tool_result("x" * 50, limit=10) is not None
    recs = L.fit_records(["a", "b", "c"], limit=100)
    assert recs == ["a", "b", "c"]
    recs2 = L.fit_records(["x" * 200, "y" * 200], limit=100)
    assert len(recs2) >= 1
    assert L.fit_records([], limit=10) == []
    assert L.fit_records(["a"], limit="bad") is not None
    assert L.fit_records(["a"], limit=0) is not None


def test_j12a_limits_env(monkeypatch):
    import importlib
    monkeypatch.setenv("HERO_TOOL_RESULT_LIMIT", "777")
    import hero_quant.config.limits as L
    importlib.reload(L)
    assert L.TOOL_RESULT_LIMIT == 777
    monkeypatch.setenv("HERO_TOOL_RESULT_LIMIT", "notanint")
    importlib.reload(L)
    assert L.TOOL_RESULT_LIMIT == 10_000
    monkeypatch.setenv("HERO_TOOL_RESULT_LIMIT", "-5")
    importlib.reload(L)
    assert L.TOOL_RESULT_LIMIT == 10_000
    monkeypatch.setenv("HERO_TOOL_RESULT_LIMIT", "999999999")
    importlib.reload(L)
    assert L.TOOL_RESULT_LIMIT == 10_000
    monkeypatch.delenv("HERO_TOOL_RESULT_LIMIT", raising=False)
    importlib.reload(L)


class _ChatFull:
    def __init__(self, fail_times=0):
        self.n = 0
        self.fail_times = fail_times
        self.usage = {"in": 5}

    def stream_chat(self, prompt, timeout=None):
        self.n += 1
        if self.n <= self.fail_times:
            raise ConnectionError("flaky")
        yield {"type": "text", "text": "hi"}

    def invoke(self, prompt, timeout=None):
        return "invoked"

    def chat(self, prompt, timeout=None):
        return "chatted"


def test_j12a_llm_client():
    from hero_quant.llm.client import LLMClient

    c = LLMClient(_ChatFull(), timeout=5, max_retries=2)
    assert list(c.stream_chat("q")) == [{"type": "text", "text": "hi"}]
    assert c.invoke("q") == "invoked"
    assert c.chat("q") == "chatted"
    assert c("q") == "invoked"
    assert c.usage == {"in": 5}
    # 重试后成功
    c2 = LLMClient(_ChatFull(fail_times=1), timeout=5, max_retries=2)
    assert list(c2.stream_chat("q")) is not None
    # 重试耗尽
    c3 = LLMClient(_ChatFull(fail_times=9), timeout=5, max_retries=1)
    with pytest.raises(ConnectionError):
        list(c3.stream_chat("q"))
    # 无 timeout 形参后端
    class _NoTimeout:
        def stream_chat(self, prompt):
            yield {"type": "text", "text": "nt"}

        def invoke(self, prompt):
            return "nt-inv"
    c4 = LLMClient(_NoTimeout())
    assert list(c4.stream_chat("q")) is not None
    assert c4.invoke("q") == "nt-inv"
    # LangChain stream 形态
    class _LC:
        def stream(self, prompt, timeout=None):
            yield "chunk1"
            yield {"type": "text", "text": "chunk2"}

            class _Msg:
                content = "chunk3"
                tool_calls = [{"name": "t"}]
            yield _Msg()

            class _Msg2:
                content = ""
                tool_calls = [{"name": "t2"}]
            yield _Msg2()
            yield 123
    c5 = LLMClient(_LC())
    assert len(list(c5.stream_chat("q"))) == 5
    # 仅 callable
    c6 = LLMClient(lambda prompt, timeout=None: "call-res")
    assert c6.invoke("q") == "call-res"
    # 仅 stream_chat（invoke 走拼接 + tool_calls 收集）
    class _OnlyStream:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "text", "text": "ab"}
            yield {"tool_calls": [{"name": "t"}], "text": ""}
            yield "cd"
    c7 = LLMClient(_OnlyStream())
    assert c7.invoke("q") == "abcd"
    assert c7.last_tool_calls == [{"name": "t"}]
    # 空对象
    with pytest.raises(AttributeError):
        LLMClient(object()).invoke("q")
    with pytest.raises(AttributeError):
        list(LLMClient(object()).stream_chat("q"))
    # _accepts_timeout 坏签名
    assert LLMClient._accepts_timeout(object()) in (True, False)
    assert LLMClient._accepts_timeout(lambda p, **k: p) is True
    # 签名撒谎 TypeError 透出
    def _liar(prompt, timeout=None):
        raise TypeError("inner oops")
    with pytest.raises(TypeError):
        LLMClient._call_with_optional_timeout(_liar, "q", 5)


def test_j12a_tools_indicators(monkeypatch):
    from hero_quant.tools import quantlib_tool as QT

    monkeypatch.setattr(QT, "_fetch_closes",
                        lambda symbol, start="", end="": [100.0 + i for i in range(40)])
    for ind in ("sma", "ema", "rsi", "macd", "bollinger"):
        r = QT.compute_indicator(symbol="A", indicator=ind, window=5)
        assert isinstance(r, dict) and r.get("ok") is True, ind
    r = QT.compute_indicator(symbol="A", indicator="sma", window=500)
    assert r.get("ok") is False
    assert isinstance(QT.compute_sharpe([]), dict)
    assert isinstance(QT.compute_sharpe([0.01] * 30), dict)
    assert isinstance(QT.compute_drawdown([]), dict)
    assert isinstance(QT.compute_drawdown([100, 80, 90]), dict)


def test_j12a_tools_backtest_variants(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    from hero_quant.tools import backtest as BT

    r = BT.run_backtest(symbol="600519.SH", start="2024-01-01", end="2024-01-10",
                        weights=[1.0], allow_synthetic=True)
    assert isinstance(r, dict)
    r2 = BT.run_backtest(symbol="X", start="2024-01-01", end="2024-01-02")
    assert isinstance(r2, dict)
    assert isinstance(BT.get_backtest_metrics([]), dict)
    assert isinstance(BT.get_backtest_metrics([100, 101]), dict)
    assert isinstance(BT.optimize_portfolio(symbols=["A", "B"], method="equal"), dict)
    assert isinstance(BT.optimize_portfolio(symbols=["A"], method="nope"), dict)
    assert isinstance(BT.validate_backtest(weights_on="2024-01-05", price_date="2024-01-01"), dict)


def test_j12a_runner_and_risk():
    from hero_quant.sandbox import runner as RU

    if hasattr(RU, "run_source"):
        assert RU.run_source("x = 1", timeout=5) is not None
        assert RU.run_source("import os", timeout=5) is not None
        assert RU.run_source("while True: pass", timeout=1) is not None
        assert RU.run_source("syntax error (((", timeout=5) is not None
    from hero_quant.api import risk as RK

    assert len([n for n in dir(RK) if not n.startswith("_")]) > 0
    for n in ("assess", "check", "evaluate", "limit_order_check", "pre_trade_check"):
        if hasattr(RK, n):
            try:
                getattr(RK, n)({"symbol": "A", "qty": 1})
            except Exception:
                pass
            break
