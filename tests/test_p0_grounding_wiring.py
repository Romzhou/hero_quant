"""P0-1 接线测试：工具执行路径 -> GroundingLedger.ingest -> assert_price 闭环。

缺陷本体（P0-1）：`hero_quant.agent.loop.AgentLoop.run` 每轮末调
`grounding.assert_price` 校验 buffer，但 `GroundingLedger.ingest` 在 src/ 内
零调用 —— 账本恒空 -> `symbols == []` -> `grounding_verified = False` ->
进纠正轮 -> 轮次耗尽 `grounding_failed`。`api/server.py::query` 里
"真实 ingest 由工具执行路径负责" 当时是假注释（无任何调用方）。

本文件按 AGENTS §11 的接线三层锁定这一条线：
- 第 2 层 来源精确匹配：账本必须是 `hero_quant.agent.grounding.GroundingLedger`；
- 第 3 层 行为探针：工具必须来自真实 `TOOL_REGISTRY`，循环必须是真实
  `AgentLoop.run`，账本必须真的能吃到一个只有正确实现才具备的可观测副作用
  （evidence 里有工具返回的 low/high/provenance）；
- 唯一替身是外部模型（脚本化 LLM），不含任何内部实现 mock。

夹具设计说明（为什么第 1 轮故意给区间外价格、为什么要把 bars 数值挡在回显窗口外）：
`AgentLoop._handle_result` 会把工具结果回显进 buffer，而接地校验是
"buffer 里任意一个价格命中证据即通过"（`assert_price` 逐个 try）。因此若工具的
bars 数值出现在回显里，本轮就会自证通过，无法区分"ingest 生效"与"回显自证"。
故夹具用一个前置的 `note` 键把数值挤到 `redacted_result_str[:2000]` 窗口之外，
并让第 1 轮文本给区间外价（999.0）强制进纠正轮；第 2 轮文本给区间内价（118.0）。
这样 `grounding_verified is True` 的唯一解释就是"工具结果真的进了账本"。
（该自证缺陷是存量问题，不在本次接线范围内，已在报告里列为遗留风险。）
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hero_quant.agent.grounding import GroundingError, GroundingLedger
from hero_quant.agent.loop import AgentLoop
from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

_SYM = "P0WIRE"
_SYM_B = "P0SEC"
_TOOL = "p0_grounding_bars"
_BATCH_TOOL = "p0_grounding_bars_batch"
_MAP_TOOL = "p0_grounding_bars_map"
_GUARD_TOOL = "p0_grounding_bars_not_ok"
_BAD_TOOL = "p0_grounding_bars_bad_prov"

_PROV = {"source": "test", "unit": "shares"}

# 两根连续工作日 bars（2026-08-03 周一 / 2026-08-04 周二），证据区间 [98.0, 120.0]
_BARS = [
    {"date": "2026-08-03", "open": 100.0, "high": 112.0, "low": 98.0, "close": 110.0, "volume": 1000},
    {"date": "2026-08-04", "open": 110.0, "high": 120.0, "low": 109.0, "close": 118.0, "volume": 1200},
]
_BARS_B = [
    {"date": "2026-08-03", "open": 50.0, "high": 52.0, "low": 48.0, "close": 51.0, "volume": 500},
    {"date": "2026-08-04", "open": 51.0, "high": 53.0, "low": 49.0, "close": 52.0, "volume": 600},
]
# 把 bars 数值挡在 2000 字回显窗口之外（纯字母短词，避开密钥类脱敏正则）
_PAD = " ".join(["pad"] * 600)


def _register(monkeypatch, name: str, func, *, safe: bool = True) -> ToolSpec:
    """把确定性工具注册进真实 TOOL_REGISTRY（用例结束由 monkeypatch 还原）。"""
    spec = ToolSpec(
        name=name,
        description=f"p0 grounding wiring tool {name}",
        func=func,
        is_concurrency_safe=lambda args: safe,
    )
    monkeypatch.setitem(TOOL_REGISTRY, name, spec)
    return spec


def _payload(bars=None, prov=None, ok: bool = True, **extra):
    """模拟 tools/market_data.py::get_market_data 的返回形状（数值后置 + 前置 pad）。"""
    body = {"note": _PAD, "bars": list(bars if bars is not None else _BARS)}
    body["provenance"] = dict(prov if prov is not None else _PROV)
    body["ok"] = ok
    body.update(extra)
    return body


class _ScriptedLLM:
    """按轮脚本产出 chunk 的外部模型替身（第 N 次调用取第 N 段脚本，末段可复用）。"""

    def __init__(self, turns):
        self._turns = [list(t) for t in turns]
        self.calls = 0

    def stream_chat(self, goal):
        idx = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        yield from self._turns[idx]


class _CollectingTrace:
    """最小 trace writer（append/path 鸭子类型），用于断言 ingest 事件落盘。"""

    def __init__(self, tmp_path):
        self.path = Path(tmp_path) / "trace.jsonl"
        self.events = []

    def append(self, event):
        self.events.append(event)


def _text_chunk(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_call(name: str, args: dict) -> dict:
    return {"type": "tool_call", "name": name, "arguments": args}


# ---------------------------------------------------------------- 夹具前提


def test_fixture_bars_are_weekday_business_days():
    """两条 bars 必须是工作日（避免"周末合成 K 线"混进接地证据）。"""
    assert [b["date"] for b in _BARS] == ["2026-08-03", "2026-08-04"]
    assert all(date.fromisoformat(b["date"]).weekday() < 5 for b in _BARS)


# ---------------------------------------------------------------- 正向：ingest 生效


def test_tool_result_bars_are_ingested_and_grounded_text_completes(monkeypatch, tmp_path):
    """工具返回的 bars 进账本后，第 2 轮区间内价格可通过校验并正常完成（无纠正轮）。"""
    ledger = GroundingLedger()
    tool_calls = {"n": 0}

    def _bars_tool(symbol: str):
        tool_calls["n"] += 1
        assert symbol == _SYM
        return _payload()

    _register(monkeypatch, _TOOL, _bars_tool, safe=True)
    trace = _CollectingTrace(tmp_path)
    llm = _ScriptedLLM(
        [
            [_text_chunk(f"{_SYM} 现价 999.0"), _tool_call(_TOOL, {"symbol": _SYM})],
            [_text_chunk(f"{_SYM} 收盘价 118.0")],
        ]
    )
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=trace)
    res = loop.run(f"{_SYM} 现在多少钱？")

    assert tool_calls["n"] == 1
    assert llm.calls == 2, "第 2 轮命中证据即终止，不得进第 3 轮纠正"

    # 来源精确匹配 + 行为探针：循环用的是注入的真实账本，且账本吃到了工具返回的 bars
    assert loop.grounding is ledger
    assert type(ledger).__module__ == "hero_quant.agent.grounding"
    assert _SYM in ledger._evidence, "工具执行路径没有把 bars ingest 进账本（P0-1）"
    ev = ledger._evidence[_SYM]
    assert ev["low"] == pytest.approx(98.0)
    assert ev["high"] == pytest.approx(120.0)
    assert ev["provenance"] == _PROV
    assert [b["close"] for b in ev["bars"]] == [pytest.approx(110.0), pytest.approx(118.0)]

    # 夹具前提自检：bars 数值没有被回显进 buffer，故通过只能归因于 ingest
    assert "98.0" not in res.text
    assert "120.0" not in res.text

    assert res.reason == "completed"
    assert res.terminated is True
    assert res.grounding_verified is True

    ingest_events = [e for e in trace.events if e.get("type") == "grounding_ingest"]
    assert len(ingest_events) == 1
    assert ingest_events[0].get("symbol") == _SYM
    assert ingest_events[0].get("bars") == 2


# ---------------------------------------------------------------- 负例：assert 未被绕过


def test_out_of_range_text_price_is_not_verified(monkeypatch, tmp_path):
    """区间外文本价格不得被判定为已接地（证明 ingest 接线没有绕过 assert_price）。"""
    ledger = GroundingLedger()
    tool_calls = {"n": 0}

    def _bars_tool(symbol: str):
        tool_calls["n"] += 1
        return _payload()

    _register(monkeypatch, _TOOL, _bars_tool, safe=True)
    llm = _ScriptedLLM(
        [
            [_text_chunk(f"{_SYM} 现价 999.0"), _tool_call(_TOOL, {"symbol": _SYM})],
            [_text_chunk(f"{_SYM} 收盘价 999.0")],
        ]
    )
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=_CollectingTrace(tmp_path))
    res = loop.run(f"{_SYM} 现在多少钱？")

    # ingest 确实发生了（否则这条负例只是"账本为空"，证明不了 assert 生效）
    assert _SYM in ledger._evidence
    assert tool_calls["n"] == 1
    assert llm.calls == 2
    with pytest.raises(GroundingError, match="not in evidence"):
        ledger.assert_price(_SYM, 999.0)

    assert res.grounding_verified is False
    assert res.reason == "grounding_failed"


# ---------------------------------------------------------------- 批量形状


def test_batch_data_result_ingests_each_symbol(monkeypatch, tmp_path):
    """get_bars_range 的 {"data": {sym: {...}}, "ok": bool} 形状逐 symbol 入账（串行路径）。"""
    ledger = GroundingLedger()

    def _batch_tool(symbols: list):
        assert symbols == [_SYM, _SYM_B]
        return {
            "data": {
                _SYM: {"bars": list(_BARS), "provenance": dict(_PROV), "ok": True},
                _SYM_B: {"bars": list(_BARS_B), "provenance": dict(_PROV), "ok": True},
            },
            "ok": True,
        }

    _register(monkeypatch, _BATCH_TOOL, _batch_tool, safe=False)
    llm = _ScriptedLLM([[_tool_call(_BATCH_TOOL, {"symbols": [_SYM, _SYM_B]})]])
    loop = AgentLoop(llm=llm, max_iterations=1, grounding=ledger, trace=False)
    res = loop.run("批量看两只票")

    assert res.terminated is True
    assert set(ledger._evidence) == {_SYM, _SYM_B}
    assert ledger._evidence[_SYM]["low"] == pytest.approx(98.0)
    assert ledger._evidence[_SYM_B]["high"] == pytest.approx(53.0)


def test_bare_symbol_mapping_result_ingests_each_symbol(monkeypatch):
    """裸映射 {sym: {"bars","provenance","ok"}} 同样逐 symbol 入账。"""
    ledger = GroundingLedger()

    def _map_tool(**_kwargs):
        return {_SYM_B: {"bars": list(_BARS_B), "provenance": dict(_PROV), "ok": True}}

    _register(monkeypatch, _MAP_TOOL, _map_tool, safe=False)
    llm = _ScriptedLLM([[_tool_call(_MAP_TOOL, {})]])
    loop = AgentLoop(llm=llm, max_iterations=1, grounding=ledger, trace=False)
    loop.run("看一只票")

    assert set(ledger._evidence) == {_SYM_B}
    assert ledger._evidence[_SYM_B]["provenance"] == _PROV


def test_batch_symbol_with_ok_false_is_skipped(monkeypatch):
    """批量里逐 symbol ok=False（合成回退）不得入账，ok=True 的仍然入账。"""
    ledger = GroundingLedger()

    def _batch_tool(symbols: list):
        return {
            "data": {
                _SYM: {"bars": list(_BARS), "provenance": dict(_PROV), "ok": True},
                _SYM_B: {"bars": list(_BARS_B), "provenance": dict(_PROV), "ok": False},
            },
            "ok": False,
        }

    _register(monkeypatch, _BATCH_TOOL, _batch_tool, safe=False)
    llm = _ScriptedLLM([[_tool_call(_BATCH_TOOL, {"symbols": [_SYM, _SYM_B]})]])
    loop = AgentLoop(llm=llm, max_iterations=1, grounding=ledger, trace=False)
    loop.run("批量看两只票")

    assert set(ledger._evidence) == {_SYM}


# ---------------------------------------------------------------- fail-closed 与容错


def test_ok_false_result_is_not_ingested(monkeypatch):
    """顶层 ok=False（合成回退）不得进证据账本 —— 假数据绝不能当 live 用。"""
    ledger = GroundingLedger()

    def _not_ok_tool(symbol: str):
        return _payload(ok=False, error="network down")

    _register(monkeypatch, _GUARD_TOOL, _not_ok_tool, safe=False)
    llm = _ScriptedLLM([[_tool_call(_GUARD_TOOL, {"symbol": _SYM})], [_text_chunk("暂无数据")]])
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False)
    loop.run("看一只票")

    assert ledger._evidence == {}


def test_symbol_falls_back_to_provenance(monkeypatch):
    """args 缺 symbol 时用 provenance["symbol"] 入账（工具漏参不算丢证据）。"""
    ledger = GroundingLedger()
    prov = {**_PROV, "symbol": _SYM}

    def _no_symbol_tool(**_kwargs):
        return _payload(prov=prov)

    _register(monkeypatch, _TOOL, _no_symbol_tool, safe=False)
    llm = _ScriptedLLM([[_tool_call(_TOOL, {})]])
    loop = AgentLoop(llm=llm, max_iterations=1, grounding=ledger, trace=False)
    loop.run("看一只票")

    assert set(ledger._evidence) == {_SYM}


def test_ingest_error_is_narrow_and_loop_continues(monkeypatch, tmp_path):
    """provenance 非法 -> ingest 抛 GroundingError -> 窄捕获 + trace 事件 + 循环不中断。"""
    ledger = GroundingLedger()

    def _bad_prov_tool(symbol: str):
        return _payload(prov={"source": "test", "unit": "公斤"})

    _register(monkeypatch, _BAD_TOOL, _bad_prov_tool, safe=False)
    trace = _CollectingTrace(tmp_path)
    llm = _ScriptedLLM(
        [
            [_text_chunk(f"{_SYM} 现价 118.0"), _tool_call(_BAD_TOOL, {"symbol": _SYM})],
            [_text_chunk("工具数据不可用")],
        ]
    )
    loop = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=trace)
    res = loop.run(f"{_SYM} 现在多少钱？")

    # 账本仍是空的（非法 provenance 被拒），但循环跑完两轮没有抛异常
    assert ledger._evidence == {}
    assert llm.calls == 2
    assert res.terminated is True
    err_events = [e for e in trace.events if e.get("type") == "grounding_ingest_error"]
    assert len(err_events) == 1
    assert err_events[0].get("error_code") == "grounding_ingest_error"
    assert err_events[0].get("tool") == _BAD_TOOL
    # 工具本身成功（ok=True、err=None），所以不是 tool_error，而是 ingest 被拒
    assert "tool_error" not in res.text
