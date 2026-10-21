"""W4 覆盖率补齐：agent/loop.py 主循环的工具执行 / 接地校验 / 预算熔断分支。

覆盖目标（src/hero_quant/agent/loop.py）：
- 工具调用解析：tool/function/args 多种包裹形态、JSON 字符串参数、坏 JSON 降级
- 工具执行：并发安全工具、未知工具（tool_not_found）、工具异常（tool_error）、
  超时（timeoutMs -> tool_error timeout）
- 漏参回填：goal 中的 symbol 回填到 get_market_data、run_backtest 缺省 600519.SH
- 批冻结：工具签名含 authorized 时注入 evidence 快照
- 接地校验：价格越界 -> 纠正轮 -> grounding_failed；无数字断言 -> 通过
- 预算熔断：should_fallback 为真 -> budget_fallback；估算异常 -> fail-closed 熔断
- 纯文本完成路径与空输出继续路径

断言均为具体文本片段/状态/reason/调用参数，不使用弱断言。
"""

from __future__ import annotations


import pytest

from hero_quant.agent.grounding import GroundingLedger
from hero_quant.agent.loop import AgentLoop
from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

_PROV = {"source": "test", "unit": "shares"}


def _register(monkeypatch, name: str, func, *, safe: bool = True, timeout_ms: int | None = None) -> ToolSpec:
    """把一个工具直接注册进 TOOL_REGISTRY（用例结束自动还原）。"""
    spec = ToolSpec(
        name=name,
        description=f"w4 test tool {name}",
        func=func,
        is_concurrency_safe=(lambda args: safe) if isinstance(safe, bool) else safe,
        timeoutMs=timeout_ms,
    )
    monkeypatch.setitem(TOOL_REGISTRY, name, spec)
    return spec


class _ToolLLM:
    """按脚本产出 chunk 的假 LLM。"""

    def __init__(self, chunks_per_call):
        self._chunks_per_call = list(chunks_per_call)
        self.calls = 0

    def stream_chat(self, goal):
        idx = min(self.calls, len(self._chunks_per_call) - 1)
        self.calls += 1
        for c in self._chunks_per_call[idx]:
            yield c


def _tool_call(name: str, args=None, **extra):
    chunk = {"type": "tool_call", "name": name, "arguments": args if args is not None else {}}
    chunk.update(extra)
    return chunk


# ---------------------------------------------------------------- 工具解析与执行


def test_registered_tool_is_executed_and_result_recorded(monkeypatch):
    """已注册只读工具被执行，结果写入输出并正常完成。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return {"close": 100.0}

    _register(monkeypatch, "w4_read_tool", _fn)
    llm = _ToolLLM([[_tool_call("w4_read_tool", {"a": 1})]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    res = loop.run("do it")
    assert seen == [{"a": 1}]
    assert "[tool w4_read_tool result]" in res.text
    assert res.reason == "completed"
    assert res.terminated is True


def test_unknown_tool_is_reported_as_not_found(monkeypatch):
    """未注册工具不执行，输出 tool_not_found（工具失败不视为完成，续跑到轮次上限）。"""
    llm = _ToolLLM([[_tool_call("w4_missing_tool", {})]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    res = loop.run("go")
    assert "tool_not_found: w4_missing_tool" in res.text
    assert res.reason == "max_iterations"


def test_tool_exception_becomes_tool_error(monkeypatch):
    """工具抛异常 -> 输出 tool_error（不中断主循环）。"""

    def _fn(**_kwargs):
        raise RuntimeError("boom")

    _register(monkeypatch, "w4_boom_tool", _fn)
    llm = _ToolLLM([[_tool_call("w4_boom_tool", {})]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    res = loop.run("go")
    assert "tool_error: boom" in res.text


def test_tool_args_as_json_string_is_parsed(monkeypatch):
    """参数为 JSON 字符串时被解析为 dict。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w4_json_args_tool", _fn)
    llm = _ToolLLM([[_tool_call("w4_json_args_tool", '{"x": 7}')]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    loop.run("go")
    assert seen == [{"x": 7}]


def test_tool_args_bad_json_degrades_to_empty(monkeypatch):
    """参数 JSON 非法时降级为空 dict（工具仍被调用）。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w4_bad_json_tool", _fn)
    llm = _ToolLLM([[_tool_call("w4_bad_json_tool", "{not-json")]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    loop.run("go")
    assert seen == [{}]


def test_tool_call_with_calls_list_is_parsed(monkeypatch):
    """type=tool_calls + calls 列表形态被解析出工具名与参数。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w4_wrapped_tool", _fn)
    chunk = {"type": "tool_calls", "calls": [{"name": "w4_wrapped_tool", "arguments": {"z": 3}}]}
    llm = _ToolLLM([[chunk]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    loop.run("go")
    assert seen == [{"z": 3}]


def test_tool_call_with_tool_calls_key_is_parsed(monkeypatch):
    """tool_calls 键包裹的列表形态同样被解析。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w4_tc_key_tool", _fn)
    chunk = {"tool_calls": [{"name": "w4_tc_key_tool", "arguments": {"y": 5}}]}
    llm = _ToolLLM([[chunk]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    loop.run("go")
    assert seen == [{"y": 5}]


def test_symbol_is_autofilled_from_goal(monkeypatch):
    """get_market_data 缺 symbol 时从 goal 回填 600519.SH。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "get_market_data", _fn)
    llm = _ToolLLM([[_tool_call("get_market_data", {})]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    loop.run("analyze 600519.SH please")
    assert seen and seen[0]["symbol"] == "600519.SH"


def test_run_backtest_gets_default_symbol(monkeypatch):
    """run_backtest 缺 symbol 且 goal 无候选时使用默认 600519.SH。"""
    seen: list[dict] = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "run_backtest", _fn)
    llm = _ToolLLM([[_tool_call("run_backtest", {})]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    loop.run("please run")
    assert seen and seen[0]["symbol"] == "600519.SH"


def test_authorized_snapshot_injected_into_tool(monkeypatch):
    """工具签名含 authorized 时注入冻结身份快照。"""
    seen: list[dict] = []

    def _fn(authorized=None, **kwargs):
        seen.append({"authorized": authorized})
        return "ok"

    _register(monkeypatch, "w4_auth_tool", _fn)
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    llm = _ToolLLM([[_tool_call("w4_auth_tool", {})]])
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False)
    loop.run("go")
    assert seen and seen[0]["authorized"] == frozenset({"AAPL"})


def test_tool_timeout_reports_tool_error(monkeypatch):
    """工具超时（timeoutMs）时输出 tool_error timeout 文本。"""
    import threading

    def _fn(**_kwargs):
        threading.Event().wait(1.0)
        return "late"

    _register(monkeypatch, "w4_slow_tool", _fn, timeout_ms=30)
    llm = _ToolLLM([[_tool_call("w4_slow_tool", {})]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    res = loop.run("go")
    assert "tool_error: timeout after 30ms" in res.text


# ---------------------------------------------------------------- 接地校验


def test_grounding_price_out_of_evidence_enters_correction_round(monkeypatch):
    """输出未在证据内的价格 -> 纠正轮 -> 轮次耗尽以 grounding_failed 终止。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    llm = _ToolLLM([[{"type": "text", "text": "AAPL close 9999.0"}]])
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False)
    res = loop.run("go")
    assert res.reason == "grounding_failed"
    assert res.grounding_verified is False


def test_grounding_price_in_evidence_passes(monkeypatch):
    """输出证据内价格 -> 接地校验通过。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    llm = _ToolLLM([[{"type": "text", "text": "AAPL close 100.0"}]])
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False)
    res = loop.run("go")
    assert res.reason == "completed"
    assert res.grounding_verified is True


def test_grounding_without_numbers_is_verified(monkeypatch):
    """纯文本无数字断言 -> 视为无幻觉（通过）。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    llm = _ToolLLM([[{"type": "text", "text": "AAPL looks fine"}]])
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False)
    res = loop.run("go")
    assert res.reason == "completed"
    assert res.grounding_verified is True


# ---------------------------------------------------------------- 预算熔断


def test_budget_fallback_terminates_loop(monkeypatch):
    """预算熔断器判定超限 -> reason=budget_fallback。"""

    class _Breaker:
        def should_fallback(self, cost=0.0):
            return True

        def estimate_cost(self, usage):
            return 99.0

    llm = _ToolLLM([[{"type": "text", "text": "hello"}]])
    loop = AgentLoop(llm=llm, max_iterations=3, budget_breaker=_Breaker(), trace=False)
    res = loop.run("go")
    assert res.reason == "budget_fallback"
    assert res.terminated is True


def test_budget_estimate_failure_fails_closed(monkeypatch):
    """预算估算异常时 fail-closed 熔断（不静默继续）。"""

    class _Breaker:
        def should_fallback(self, cost=0.0):
            return False

        def estimate_cost(self, usage):
            raise RuntimeError("price table broken")

    chunk = {"type": "text", "text": "hello", "usage_metadata": {"input_tokens": 100, "output_tokens": 50}}
    llm = _ToolLLM([[chunk]])
    loop = AgentLoop(llm=llm, max_iterations=3, budget_breaker=_Breaker(), trace=False)
    res = loop.run("go")
    assert res.reason == "budget_fallback"


# ---------------------------------------------------------------- 终止路径


def test_empty_output_continues_until_max_iterations(monkeypatch):
    """空输出不终止，依赖 max_iterations 收敛。"""
    llm = _ToolLLM([[{"type": "text", "text": ""}]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    res = loop.run("go")
    assert res.reason == "max_iterations"
    assert res.terminated is True
    assert llm.calls == 2


def test_plain_text_completes(monkeypatch):
    """纯文本有输出即完成。"""
    llm = _ToolLLM([[{"type": "text", "text": "final answer"}]])
    loop = AgentLoop(llm=llm, max_iterations=3, trace=False)
    res = loop.run("go")
    assert res.reason == "completed"
    assert "final answer" in res.text


def test_sharpe_metric_is_extracted_from_output(monkeypatch):
    """输出中的 sharpe 数字被抽取到 metrics。"""
    llm = _ToolLLM([[{"type": "text", "text": "sharpe 1.62"}]])
    loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
    res = loop.run("go")
    assert res.metrics == {"sharpe": pytest.approx(1.62)}
