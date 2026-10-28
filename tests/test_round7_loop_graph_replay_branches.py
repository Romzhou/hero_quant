"""Round7 X4：agent/loop.py 的图委托路径（_run_graph/use_graph）、回放短路与用量落盘分支。

覆盖目标（src/hero_quant/agent/loop.py）：
- use_graph 路径的壁时间预算：进图前熔断（不调用图）、图跑完后才超限时覆盖 reason
- _run_graph：图构建失败 -> graph_build_error、invoke 需 config 的签名回退、
  中断信号透传、图异常 -> graph_error（含 trace）、图结果 -> 文本的多种形态
- 图结果的接地校验：证据内价格通过 / 越界判失败 / 无数字通过 / 无证据判失败 /
  extract_claims 崩溃时回退正则（含越界仍判失败）
- 图文本的 sharpe 指标抽取
- 回放短路：损坏 JSON 降级为空结果、顶层用量字段归一、非数值用量归零、
  用量侧车回灌到轨迹目录、回放件结构非法时回退真实 LLM
- 主循环用量：usage 三种字段形态累计、estimate_cost/record_usage 接线、
  record_usage 异常不阻断、should_fallback 异常 fail-closed、用量侧车落盘、
  dir_path-only writer 的 trace_path 语义

断言均为具体 reason/文本/事件字段/调用参数，不使用空壳断言。
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path


import pytest

from hero_quant.agent.grounding import GroundingLedger
from hero_quant.agent.loop import AgentLoop

_PROV = {"source": "test", "unit": "shares"}


# ------------------------------------------------------------------ 夹具


def _iter_chunks(step):
    for chunk in step:
        if isinstance(chunk, BaseException):
            raise chunk
        yield chunk


class _ScriptedLLM:
    """脚本化假 LLM（脚本项为 list 的时候是流，为 Exception 表示取流即失败）。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def stream_chat(self, goal):
        idx = min(self.calls, len(self._script) - 1)
        self.calls += 1
        step = self._script[idx]
        if isinstance(step, BaseException):
            raise step
        return _iter_chunks(step)


class _Recorder:
    """trace 鸭子类型写入器（path + append 记录）；默认路径落在临时目录，避免污染仓库根。"""

    def __init__(self, path=None):
        if path is None:
            path = str(Path(tempfile.mkdtemp(prefix="round7-loop-")) / "trace.jsonl")
        self.path = path
        self.events = []

    def append(self, obj):
        self.events.append(obj)

    def first(self, type_name):
        for e in self.events:
            if e.get("type") == type_name:
                return e
        return None

    def types(self):
        return [e.get("type") for e in self.events]


class _DirOnlyWriter:
    """只有 dir_path + append 的 writer（无 path），用于验证 trace_path 的目录语义。"""

    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.events = []

    def append(self, obj):
        self.events.append(obj)


class _Graph:
    """可注入结果/异常/延迟的图桩；config 参数可选，便于测签名回退。"""

    def __init__(self, result=None, exc=None, delay=0.0, requires_config=False):
        self.result = result
        self.exc = exc
        self.delay = delay
        self.requires_config = requires_config
        self.calls = 0
        self.states = []

    def invoke(self, state, config=None):
        if self.requires_config and config is None:
            raise TypeError("invoke() missing required argument: 'config'")
        self.calls += 1
        self.states.append(state)
        if self.delay:
            threading.Event().wait(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


def _graph_loop(graph, **kwargs):
    return AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "unused"}]]), use_graph=True, graph=graph, **kwargs)


class _Retry:
    def __init__(self, max_attempts=3, retry=False):
        self.max_attempts = max_attempts
        self._retry = retry

    def should_retry(self, exc, attempt):
        return self._retry

    def sleep(self, attempt):
        return None


class _Ledger(GroundingLedger):
    """带 AAPL 证据的账本。"""

    def __init__(self):
        super().__init__()
        self.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)


# ------------------------------------------------------------------ 图路径：壁时间预算


def test_use_graph_wall_budget_exceeded_skips_graph():
    """进图前壁时间已超限：直接 wall_time_budget_exceeded，不触达图执行。"""
    graph = _Graph(result={"messages": [{"content": "never"}]})
    loop = _graph_loop(graph, wall_time_budget=1e-9, trace=False)
    res = loop.run("q")
    assert res.reason == "wall_time_budget_exceeded"
    assert res.iterations == 0
    assert graph.calls == 0


def test_graph_completes_but_wall_time_overrun_overrides_reason():
    """图执行期间超限：即使图返回 completed，结果 reason 也被覆盖为 wall_time_budget_exceeded。"""
    graph = _Graph(result={"messages": [{"content": "graph answer"}]}, delay=0.12)
    loop = _graph_loop(graph, wall_time_budget=0.05, trace=False)
    res = loop.run("q")
    assert graph.calls == 1
    assert res.text == "graph answer"
    assert res.reason == "wall_time_budget_exceeded"
    assert res.terminated is True


# ------------------------------------------------------------------ 图路径：构建/异常


def test_graph_build_failure_reports_graph_build_error(monkeypatch):
    """未注入 graph 且默认图构建失败：graph_build_error 文本 + graph_error，不抛异常。"""

    def _boom(*a, **kw):
        raise RuntimeError("graph wiring broken")

    monkeypatch.setattr("hero_quant.agent.graph.build_research_graph", _boom)
    res = AgentLoop(llm=_ScriptedLLM([[]]), use_graph=True, trace=False).run("q")
    assert res.reason == "graph_error"
    assert res.text.startswith("graph_build_error: graph wiring broken")
    assert res.iterations == 0


def test_graph_exception_is_traced_as_graph_error():
    """图执行抛异常：收敛为 graph_error 并落 graph_error trace。"""
    trace = _Recorder()
    res = _graph_loop(_Graph(exc=RuntimeError("graph boom")), trace=trace).run("q")
    assert res.reason == "graph_error"
    assert res.text == "graph_error: graph boom"
    assert trace.first("graph_error")["error"] == "graph boom"


def test_graph_keyboard_interrupt_propagates():
    """图内 KeyboardInterrupt 透传（不得被 graph_error 吞掉）。"""
    with pytest.raises(KeyboardInterrupt):
        _graph_loop(_Graph(exc=KeyboardInterrupt()), trace=False).run("q")


def test_graph_invoke_requiring_config_falls_back_to_config_call():
    """图 invoke 必须带 config 参数时，TypeError 后回退为 invoke(state, config={})。"""
    graph = _Graph(result={"messages": [{"content": "cfg answer"}]}, requires_config=True)
    res = _graph_loop(graph, trace=False).run("q")
    assert res.text == "cfg answer"
    assert res.reason == "completed"
    assert graph.calls == 1


@pytest.mark.parametrize(
    ("result", "expected_text"),
    [
        ({"msgs": ["plain last message"]}, "plain last message"),
        ({"messages": []}, "{'messages': []}"),
        ("raw-string-result", "raw-string-result"),
    ],
)
def test_graph_result_text_conversion_variants(result, expected_text):
    """图结果转文本：msgs 列表取末条（非 dict 直接 str）、空消息回退 str(result)、非 dict 直接 str。"""
    trace = _Recorder()
    res = _graph_loop(_Graph(result=result), trace=trace).run("q")
    assert res.text == expected_text
    assert trace.first("graph_result")["text"] == expected_text


def test_graph_metrics_extracted_from_text():
    """图输出文本里的 sharpe 被抽到 metrics。"""
    res = _graph_loop(_Graph(result={"messages": [{"content": "final sharpe: 2.5"}]}), trace=False).run("q")
    assert res.metrics == {"sharpe": pytest.approx(2.5)}


# ------------------------------------------------------------------ 图路径：接地校验


def test_graph_grounding_passes_for_price_in_evidence():
    """图输出价格落在证据内 -> grounding_verified True。"""
    res = _graph_loop(
        _Graph(result={"messages": [{"content": "AAPL close 100.0"}]}), grounding=_Ledger(), trace=False
    ).run("q")
    assert res.grounding_verified is True
    assert res.reason == "completed"


def test_graph_grounding_rejects_price_outside_evidence():
    """图输出价格越界 -> grounding_verified False（图路径与主循环共用同一校验口径）。"""
    res = _graph_loop(
        _Graph(result={"messages": [{"content": "AAPL close 9999.0"}]}), grounding=_Ledger(), trace=False
    ).run("q")
    assert res.grounding_verified is False


def test_graph_grounding_without_numeric_claims_passes():
    """图输出无价格断言 -> 视为无幻觉。"""
    res = _graph_loop(
        _Graph(result={"messages": [{"content": "AAPL looks fine"}]}), grounding=_Ledger(), trace=False
    ).run("q")
    assert res.grounding_verified is True


def test_graph_grounding_without_evidence_fails_on_numbers():
    """有数字断言但账本无任何证据 -> 判为未验证（fail-visible）。"""
    empty = GroundingLedger()
    res = _graph_loop(
        _Graph(result={"messages": [{"content": "AAPL close 100.0"}]}), grounding=empty, trace=False
    ).run("q")
    assert res.grounding_verified is False


def test_graph_grounding_extract_claims_failure_falls_back_to_regex(monkeypatch):
    """图路径 extract_claims 崩溃时回退旧正则，证据内价格仍校验通过。"""

    def _boom(_text):
        raise RuntimeError("claim extractor broken")

    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", _boom)
    res = _graph_loop(
        _Graph(result={"messages": [{"content": "AAPL close 100.0"}]}), grounding=_Ledger(), trace=False
    ).run("q")
    assert res.grounding_verified is True


def test_graph_grounding_regex_fallback_still_rejects_bad_price(monkeypatch):
    """回退正则路径下越界价格仍判未验证（回退不降低校验强度）。"""

    def _boom(_text):
        raise RuntimeError("claim extractor broken")

    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", _boom)
    res = _graph_loop(
        _Graph(result={"messages": [{"content": "AAPL close 9999.0"}]}), grounding=_Ledger(), trace=False
    ).run("q")
    assert res.grounding_verified is False


# ------------------------------------------------------------------ 回放短路


def _replay_loop(tmp_path, payload, *, trace=False, name="replay.json"):
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "live answer"}]]),
        replay_path=str(path),
        allow_root=str(tmp_path),
        trace=trace,
    )


def test_replay_corrupt_json_degrades_to_empty_result(tmp_path):
    """回放件 JSON 损坏：降级为空文本的 completed 结果（不抛异常）。"""
    res = _replay_loop(tmp_path, "{not-json", trace=False).run("q")
    assert res.reason == "completed"
    assert res.text == ""
    assert res.terminated is True


def test_replay_top_level_usage_fields_are_normalized(tmp_path):
    """回放件用量放在顶层（input_tokens/output_tokens）时也被归一并落 trace。"""
    trace = _Recorder()
    res = _replay_loop(tmp_path, {"input_tokens": 12, "output_tokens": 34, "text": "hi"}, trace=trace).run("q")
    assert res.text == "hi"
    assert res.token_count == len("hi") // 4
    assert trace.first("llm_usage")["llm_usage"] == {"input_tokens": 12, "output_tokens": 34}


def test_replay_non_numeric_usage_degrades_to_zero(tmp_path):
    """回放件用量字段非数值时归零（不因脏数据崩溃）。"""
    trace = _Recorder()
    _replay_loop(tmp_path, {"llm_usage": {"input_tokens": "abc", "output_tokens": None}, "text": "x"}, trace=trace).run("q")
    assert trace.first("llm_usage")["llm_usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_replay_usage_sidecar_written_into_trace_dir(tmp_path, monkeypatch):
    """回放用量回灌到轨迹目录（llm_usage.json），便于后续回放/计费核对。"""
    monkeypatch.chdir(tmp_path)
    trace_file = tmp_path / "traces" / "trace.jsonl"
    trace = _Recorder(str(trace_file))
    res = _replay_loop(tmp_path, {"llm_usage": {"input_tokens": 7, "output_tokens": 3}, "text": "hi"}, trace=trace).run("q")
    sidecar = trace_file.parent / "llm_usage.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["text"] == "hi"
    assert payload["llm_usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert res.trace_path == str(trace_file)


def test_replay_with_non_dict_payload_falls_back_to_live_llm(tmp_path):
    """回放件结构非法（JSON 顶层是数组）时回退真实 LLM 调用，而不是返回空结果。"""
    loop = _replay_loop(tmp_path, [1, 2, 3], trace=False)
    res = loop.run("q")
    assert res.text == "live answer"
    assert res.reason == "completed"
    assert loop.llm.calls == 1


# ------------------------------------------------------------------ 用量上报与侧车落盘


def test_usage_variants_accumulate_and_feed_budget_breaker():
    """usage 的三种字段形态（usage 字典 / 顶层 token 键 / 脏值）都被累计并驱动 estimate_cost。"""
    seen = {"estimate": [], "costs": [], "recorded": []}

    class _Breaker:
        def estimate_cost(self, usage):
            seen["estimate"].append(usage)
            return 0.42

        def record_usage(self, usage):
            seen["recorded"].append(usage)

        def should_fallback(self, cost=0.0):
            seen["costs"].append(cost)
            return False

    chunks = [
        {"type": "text", "text": "a", "usage": {"input_tokens": 5, "completion_tokens": 7}},
        {"type": "text", "text": "b", "input_tokens": 1},
        {"type": "text", "text": "c", "usage": {"input_tokens": "dirty"}},
        {"type": "text", "text": "d", "usage": {"prompt_tokens": 2, "generated_tokens": 3}},
        {"type": "text", "text": "e", "usage": {"output_tokens": "dirty"}},
    ]
    res = AgentLoop(llm=_ScriptedLLM([chunks]), max_iterations=2, budget_breaker=_Breaker(), trace=False).run("q")
    assert res.reason == "completed"
    # 脏值不计入（也不写入 record_usage），合法值按 input/output 口径累计
    assert seen["recorded"] == [
        {"input_tokens": 5, "output_tokens": 7},
        {"input_tokens": 1, "output_tokens": 0},
        {"input_tokens": 2, "output_tokens": 3},
    ]
    assert seen["estimate"] == [{"input_tokens": 8, "output_tokens": 10}]
    # 真实 usage 已 record_usage 累计，判定以 cost=0 读取熔断器自身状态（避免重复计价）
    assert seen["costs"] == [0]


def test_record_usage_failure_does_not_break_run():
    """熔断器 record_usage 抛异常被吞掉，主循环照常完成。"""

    class _Breaker:
        def record_usage(self, usage):
            raise RuntimeError("billing sink down")

    chunk = {"type": "text", "text": "hi", "usage_metadata": {"input_tokens": 3, "output_tokens": 4}}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, budget_breaker=_Breaker(), trace=False).run("q")
    assert res.reason == "completed"
    assert res.text == "hi"


def test_budget_should_fallback_failure_with_real_usage_fails_closed():
    """有真实 usage 时熔断判定抛异常仍 fail-closed（budget_fallback）。"""

    class _Breaker:
        def estimate_cost(self, usage):
            return 0.5

        def should_fallback(self, cost=0.0):
            raise RuntimeError("breaker broken")

    chunk = {"type": "text", "text": "hi", "usage_metadata": {"input_tokens": 3, "output_tokens": 4}}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, budget_breaker=_Breaker(), trace=False).run("q")
    assert res.reason == "budget_fallback"
    assert res.terminated is True


def test_usage_sidecar_written_next_to_duck_typed_writer(tmp_path):
    """非回放路径也把累计用量写到 trace 同目录的 llm_usage.json。"""
    trace = _Recorder(str(tmp_path / "trace.jsonl"))
    chunk = {"type": "text", "text": "final answer", "usage_metadata": {"input_tokens": 9, "output_tokens": 5}}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, trace=trace).run("q")
    sidecar = tmp_path / "llm_usage.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["llm_usage"] == {"input_tokens": 9, "output_tokens": 5}
    assert payload["text"] == "final answer"
    assert trace.first("llm_usage")["llm_usage"] == {"input_tokens": 9, "output_tokens": 5}
    assert res.trace_path == str(tmp_path / "trace.jsonl")


def test_dir_only_writer_reports_directory_as_trace_path(tmp_path):
    """只有 dir_path 的 writer：trace_path 暴露目录（而非虚构文件路径）。"""
    writer = _DirOnlyWriter(str(tmp_path))
    res = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), max_iterations=2, trace=writer).run("q")
    assert res.trace_path == str(tmp_path)
    assert writer.events  # 事件确实写入了该 writer


# ------------------------------------------------------------------ 主循环：中断与不可重试


def test_llm_keyboard_interrupt_propagates():
    """取流阶段 KeyboardInterrupt 直接透传（不被 llm_error 收敛）。"""
    llm = _ScriptedLLM([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        AgentLoop(llm=llm, max_iterations=2, retry_policy=_Retry(), trace=False).run("q")


def test_non_retryable_llm_failure_does_not_waste_attempts():
    """策略判定不可重试时立刻放弃（只调用一次 LLM），reason=llm_error。"""
    llm = _ScriptedLLM([RuntimeError("fatal")])
    res = AgentLoop(llm=llm, max_iterations=2, retry_policy=_Retry(max_attempts=3, retry=False), trace=False).run("q")
    assert res.reason == "llm_error"
    assert llm.calls == 1


def test_stream_timeout_retried_reports_timeout_metric(monkeypatch):
    """流中途超时且可重试：重试后成功，同时上报 timeout 指标（重试原因可观测）。"""
    timeouts = []
    retries = []
    monkeypatch.setattr("hero_quant.metrics.inc_llm_timeout", lambda provider="unknown": timeouts.append(provider))
    monkeypatch.setattr(
        "hero_quant.metrics.inc_llm_retry", lambda provider="unknown", reason="error": retries.append(reason)
    )
    llm = _ScriptedLLM([[TimeoutError("slow stream")], [{"type": "text", "text": "recovered"}]])
    res = AgentLoop(llm=llm, max_iterations=3, retry_policy=_Retry(max_attempts=3, retry=True), trace=False).run("q")
    assert res.text == "recovered"
    assert res.reason == "completed"
    assert timeouts == ["loop"]
    assert retries == ["TimeoutError"]
