"""批次18 P1-9：工具结果回显不得替模型的价格断言「自证」通过接地校验。

缺陷：_handle_result 把工具结果（含真实 bars 数字）回显进 buffer，而 step 7 的
接地校验是「buffer 里任一价格命中证据即整段放行」。账本真实入账后，模型在回答里
夹带幻觉价（999999.0）也会因回显里的真价格而 grounding_verified=True。
"""

from __future__ import annotations

from hero_quant.agent.grounding import GroundingLedger
from hero_quant.agent.loop import AgentLoop
from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

_SYM = "P0ECHO"
_TOOL = "p0_echo_bars"
_PROV = {"source": "test", "unit": "shares"}
_BARS = [
    {"date": "2026-08-03", "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000},
]


class _ScriptedLLM:
    def __init__(self, turns):
        self._turns = [list(t) for t in turns]
        self.calls = 0

    def stream_chat(self, goal):
        idx = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        yield from self._turns[idx]


def _text(text):
    return {"type": "text", "text": text}


def _call(args):
    return {"type": "tool_call", "name": _TOOL, "arguments": args}


def _run(monkeypatch, answer_text):
    calls = {"n": 0}

    def _tool(symbol: str):
        calls["n"] += 1
        assert symbol == _SYM
        # 不加 pad：让 bars 数值留在 redacted_result_str[:2000] 回显窗口内
        return {"bars": list(_BARS), "provenance": dict(_PROV), "ok": True}

    spec = ToolSpec(
        name=_TOOL,
        description="batch18 grounding echo tool",
        func=_tool,
        is_concurrency_safe=lambda args: True,
    )
    monkeypatch.setitem(TOOL_REGISTRY, _TOOL, spec)

    ledger = GroundingLedger()
    # 第 1 轮就同时给出价格断言与工具调用（否则首轮无数字会直接 completed，
    # 第 2 轮根本不跑，测不到回显自证）。
    llm = _ScriptedLLM([
        [_text(answer_text), _call({"symbol": _SYM})],
        [_text(answer_text)],
    ])
    loop = AgentLoop(llm=llm, grounding=ledger, max_iterations=3, trace=False)
    return loop.run(f"{_SYM} 请回答"), ledger, calls


def test_tool_echo_cannot_verify_hallucinated_price(monkeypatch):
    """回显里有真价格，模型文本里是区间外幻觉价 → 不得 verified。"""
    res, ledger, calls = _run(monkeypatch, f"{_SYM} 收盘价 999999.0")
    assert calls["n"] == 1, "工具应执行且只执行一次"
    assert _SYM in ledger._evidence, "账本必须真的入账（ingest 仍生效）"
    assert res.grounding_verified is False, (
        "工具回显的真价格替幻觉价自证通过了接地校验"
    )


def test_in_evidence_price_still_verified_without_echo(monkeypatch):
    """模型文本给出证据内价格 → 仍应 verified（不能把正常路径一起打死）。"""
    res, ledger, calls = _run(monkeypatch, f"{_SYM} 收盘价 100.0")
    assert calls["n"] == 1
    assert _SYM in ledger._evidence
    assert res.grounding_verified is True
