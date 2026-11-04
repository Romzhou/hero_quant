"""P0-4 端到端：worker 崩溃重跑同一任务，工具副作用只执行一次。

链路：AgentLoop.run -> _exec_spec -> tools.registry.execute_tool_call -> DedupStore。
红证据分两段：
1) 接线前 AgentLoop 不认 dedup_store（TypeError）；
2) 只加参数、不接 _exec_spec 时，第二次 run 仍会再执行工具（counter == 2）。
绿：两个独立 DedupStore 实例指向同一 SQLite 文件（模拟进程重启），第二次 run 重放
已存结果且 counter == 1；换 checkpoint_thread 后 counter == 2（证明不是全局缓存）。
"""

from __future__ import annotations

from pathlib import Path

from hero_quant.agent.loop import AgentLoop
from hero_quant.governance.dedup import DedupStore
from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

_TOOL = "p0_dedup_counter"
_TOKEN_V1 = "t-v1"
_TOKEN_V2 = "t-v2"


class _ScriptedLLM:
    """固定两轮：先发一次工具调用，再给最终文本。"""

    def __init__(self, token: str):
        self.token = token
        self.calls = 0

    def stream_chat(self, goal):
        self.calls += 1
        if self.calls % 2 == 1:
            yield {"type": "tool_call", "name": _TOOL, "arguments": {"token": self.token}}
        else:
            yield {"type": "text", "text": "done"}


def _register(monkeypatch, counter):
    def _tool(token: str):
        counter["n"] += 1
        return {"ok": True, "token": token, "executions": counter["n"]}

    spec = ToolSpec(
        name=_TOOL,
        description="p0 dedup loop counter",
        func=_tool,
        is_concurrency_safe=lambda args: False,
    )
    monkeypatch.setitem(TOOL_REGISTRY, _TOOL, spec)


def _run_once(store, thread: str, token: str):
    loop = AgentLoop(
        llm=_ScriptedLLM(token),
        max_iterations=3,
        trace=False,
        dedup_store=store,
        checkpoint_thread=thread,
    )
    return loop.run(f"dedup e2e {token}")


def test_worker_restart_replays_without_second_side_effect(monkeypatch, tmp_path):
    counter = {"n": 0}
    _register(monkeypatch, counter)
    db = Path(tmp_path) / "dedup.db"

    first = _run_once(DedupStore(db_path=db), "p0-task-1", _TOKEN_V1)
    assert first.reason == "completed"
    assert counter["n"] == 1, "第一次运行应执行一次工具"

    # 模拟 worker 崩溃后新进程：新 DedupStore 实例，同一 SQLite 文件与同一任务线程
    second = _run_once(DedupStore(db_path=db), "p0-task-1", _TOKEN_V1)
    assert second.reason == "completed"
    assert counter["n"] == 1, f"同一 tool_call 重放必须只执行一次，实际 {counter['n']} 次"


def test_different_thread_executes_again(monkeypatch, tmp_path):
    counter = {"n": 0}
    _register(monkeypatch, counter)
    db = Path(tmp_path) / "dedup.db"

    _run_once(DedupStore(db_path=db), "p0-task-1", _TOKEN_V2)
    _run_once(DedupStore(db_path=db), "p0-task-2", _TOKEN_V2)
    assert counter["n"] == 2, "不同任务线程不得共享幂等键"
