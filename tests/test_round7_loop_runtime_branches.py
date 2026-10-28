"""Round7 X4：agent/loop.py 主循环 run() 的兜底/降级分支（不触及 src 行为）。

覆盖目标（src/hero_quant/agent/loop.py 的 run()/_warn/inject/_writeback）：
- F 线告警助手 `_warn`：stdlib logging 为主、structlog 为辅，任一失败都不得抛出
- 历史 kwargs 别名（contextManager/budgetBreaker/retryPolicy/memoryStore/skillsLoader/replay_file）
- trace 模块不可用时显式关闭语义仍然生效
- 壁时间预算：首轮前熔断、收尾才超限时覆盖 reason（completed -> wall_time_budget_exceeded）
- 用户停止：request_stop / llm.should_stop 回调（含回调抛异常时忽略）
- LLM 适配器签名兼容（仅 prompt= 关键字）、重试耗尽（llm_error / llm_timeout）、
  should_retry/sleep 自身异常时按 attempt 预算回退
- 流中途异常：可重试 -> 回滚 partial 输出与用量（不双计费）；不可重试 -> 收敛为 llm_error
- 畸形工具调用归一化：字符串 tc、tool/args 键、function 包裹、args 非 dict、无 name 丢弃、
  content+tool_calls 不拼文本、非 dict chunk（对象/delta）
- 并发安全判定异常时退化为串行执行；工具内 KeyboardInterrupt 透传
- 接地校验：extract_claims 崩溃回退正则、证据非 dict 时授权快照为空
- 上下文压缩：truncated 时 banner 前置 / 非 truncated 仅落 trace / compact 抛异常不中断
- 预算熔断：should_fallback 抛异常 fail-closed、无 should_fallback 时按旧公式不熔断、
  无 estimate_cost 时按环境变量定价并 record_usage
- 记忆回写失败、trace 写入失败都不阻断主循环
- token 上限三个位置：流中、迭代末（工具输出膨胀）、轮次前（上下文膨胀）

断言均为具体 reason/文本片段/trace 事件/调用参数，不使用 is not None 之类的空壳断言。
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading

import pytest

from hero_quant.agent import loop as loop_mod
from hero_quant.agent.context import CompactResult
from hero_quant.agent.grounding import GroundingLedger
from hero_quant.agent.loop import AgentLoop, estimate_tokens
from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

_PROV = {"source": "test", "unit": "shares"}


# ------------------------------------------------------------------ 夹具


def _iter_chunks(step):
    """把脚本项变成流：项内的 Exception 代表"流中途"失败。"""
    for chunk in step:
        if isinstance(chunk, BaseException):
            raise chunk
        yield chunk


class _ScriptedLLM:
    """按脚本产出 chunk 的假 LLM。

    脚本项为 list 时作为该次调用的流（项内 Exception 表示流中途失败）；
    脚本项为 Exception 时表示"取流即失败"（_call_llm 阶段）。
    """

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
    """trace 鸭子类型写入器：path + append（记录事件）；默认路径落临时目录，避免污染仓库根。"""

    def __init__(self, path=None):
        if path is None:
            path = os.path.join(tempfile.mkdtemp(prefix="round7-loop-"), "trace.jsonl")
        self.path = path
        self.events = []

    def append(self, obj):
        self.events.append(obj)

    def types(self):
        return [e.get("type") for e in self.events]

    def first(self, type_name):
        for e in self.events:
            if e.get("type") == type_name:
                return e
        return None


class _BrokenWriter:
    """trace 写入恒失败的写入器，用于验证 trace 故障不阻断主循环。"""

    def __init__(self, path="broken.jsonl"):
        self.path = path
        self.attempts = 0

    def append(self, obj):
        self.attempts += 1
        raise OSError("disk full")


def _register(monkeypatch, name, func, *, safe=False, timeout_ms=None):
    spec = ToolSpec(
        name=name,
        description=f"round7 tool {name}",
        func=func,
        is_concurrency_safe=(lambda args: safe) if isinstance(safe, bool) else safe,
        timeoutMs=timeout_ms,
    )
    monkeypatch.setitem(TOOL_REGISTRY, name, spec)
    return spec


class _Retry:
    """可编程重试策略桩（不真睡眠）。"""

    def __init__(self, *, max_attempts=2, retry=True, should_retry_raises=False, sleep_raises=False):
        self.max_attempts = max_attempts
        self._retry = retry
        self._should_raise = should_retry_raises
        self._sleep_raises = sleep_raises
        self.slept = []

    def should_retry(self, exc, attempt):
        if self._should_raise:
            raise RuntimeError("policy broken")
        return self._retry

    def sleep(self, attempt):
        self.slept.append(attempt)
        if self._sleep_raises:
            raise RuntimeError("sleep broken")


class _FakeContext:
    """最小上下文管理器：可控 compact 结果/异常。"""

    def __init__(self, messages=None, compact_result=None, compact_raises=None, add_raises=False):
        self._messages = list(messages or [])
        self._compact_result = compact_result
        self._compact_raises = compact_raises
        self._add_raises = add_raises
        self.added = []
        self.compact_calls = 0

    def add(self, role, content):
        if self._add_raises:
            raise RuntimeError("context add broken")
        self.added.append((role, content))

    def compact(self):
        self.compact_calls += 1
        if self._compact_raises is not None:
            raise self._compact_raises
        return self._compact_result


# ------------------------------------------------------------------ _warn 告警助手


def test_warn_helper_logs_stdlib_warning_and_structlog_fields(caplog, monkeypatch):
    """_warn 必须同时产出 stdlib warning（caplog 可见）与 structlog 结构化字段。"""
    seen = []

    class _Slog:
        def warning(self, op, **fields):
            seen.append((op, fields))

    monkeypatch.setattr(loop_mod, "_slog", _Slog(), raising=False)
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        loop_mod._warn("probe_op", "probe failed error_code=probe_op: %s", "why")
    assert "probe failed error_code=probe_op: why" in caplog.text
    assert seen[0][0] == "probe_op"
    # chain 默认走 A 线；error_code 与 op 同名，便于检索
    assert seen[0][1]["error_code"] == "probe_op"
    assert seen[0][1]["chain"] == "A"


def test_warn_helper_swallows_logger_failures(monkeypatch):
    """两个日志通道都坏掉时 _warn 也不许抛出（否则 best-effort 告警会反噬主流程）。"""

    class _BoomSlog:
        def warning(self, *a, **kw):
            raise RuntimeError("structlog broken")

    class _BoomLogger:
        def warning(self, *a, **kw):
            raise RuntimeError("logging broken")

    monkeypatch.setattr(loop_mod, "_slog", _BoomSlog(), raising=False)
    monkeypatch.setattr(loop_mod.logging, "getLogger", lambda *a, **kw: _BoomLogger())
    try:
        loop_mod._warn("probe_op", "never raises")  # 不抛即通过
    finally:
        # logging 是全局模块，立即还原避免污染 pytest 自身的日志插件
        monkeypatch.undo()


# ------------------------------------------------------------------ 构造期兼容分支


def test_legacy_kwargs_aliases_are_wired(tmp_path):
    """驼峰/历史别名（contextManager/budgetBreaker/retryPolicy/memoryStore/skillsLoader/replay_file）都被接上。"""
    ctx = _FakeContext()
    breaker = object()
    retry = object()
    store = object()
    loader = object()
    monkeypatch_replay = tmp_path / "replay.json"
    monkeypatch_replay.write_text('{"text": "x"}', encoding="utf-8")

    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        contextManager=ctx,
        budgetBreaker=breaker,
        retryPolicy=retry,
        memoryStore=store,
        skillsLoader=loader,
        replay_file=str(monkeypatch_replay),
        allow_root=str(tmp_path),
        trace=False,
    )
    assert loop.context_manager is ctx
    assert loop.context is ctx
    assert loop.budget_breaker is breaker
    assert loop.retry_policy is retry
    assert loop.memory_store is store
    assert loop.skills_loader is loader
    assert str(loop.replay_path) == str(monkeypatch_replay.resolve())


def test_trace_optout_still_works_when_trace_module_unavailable(monkeypatch):
    """trace 模块探测失败时，显式 trace=False 仍必须关闭回灌（fail-safe 语义不变）。"""

    def _boom(*a, **kw):
        raise RuntimeError("trace module broken")

    monkeypatch.setattr("hero_quant.agent.trace.is_trace_disabled", _boom)
    loop = AgentLoop(llm=_ScriptedLLM([[]]), trace=False)
    assert loop._ensure_trace_writer() is None


def test_replay_allow_root_filesystem_root_is_rejected(tmp_path):
    """allow_root 指向文件系统根（过宽）时被拒，replay 路径 fail-closed。"""
    replay = tmp_path / "r.json"
    replay.write_text('{"text": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="outside allowed directory"):
        AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), allow_root="/", trace=False)


def test_replay_allow_root_invalid_object_falls_back_to_default_dir(tmp_path, monkeypatch):
    """allow_root 非路径对象时告警并回退默认 replays 根（回放件在 replays 下仍可读）。"""
    monkeypatch.chdir(tmp_path)
    replays = tmp_path / "replays"
    replays.mkdir()
    replay = replays / "r.json"
    replay.write_text('{"text": "replayed"}', encoding="utf-8")

    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), allow_root=object(), trace=False)
    assert loop.run("q").text == "replayed"


def test_replay_allow_root_named_replays_dir_is_trusted(tmp_path, monkeypatch):
    """部署约定：allow_root 指向名为 replays 的目录时被信任（不受 cwd/临时目录限制）。"""
    import tempfile

    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    trusted = tmp_path / "elsewhere" / "replays"
    trusted.mkdir(parents=True)
    replay = trusted / "r.json"
    replay.write_text('{"text": "trusted replay"}', encoding="utf-8")
    # 让系统临时目录探测必然失败，从而落到 "目录名 == replays" 的白名单分支
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "othertmp"))

    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), allow_root=str(trusted), trace=False)
    assert loop.run("q").text == "trusted replay"


# ------------------------------------------------------------------ 壁时间预算


def test_wall_time_budget_exceeded_before_first_iteration(tmp_path):
    """壁时间预算在首轮前即耗尽 -> wall_time_budget_exceeded，且不调用 LLM。"""
    trace = _Recorder(str(tmp_path / "t.jsonl"))
    llm = _ScriptedLLM([[{"type": "text", "text": "unused"}]])
    loop = AgentLoop(llm=llm, wall_time_budget=1e-9, trace=trace)
    res = loop.run("q")
    assert res.reason == "wall_time_budget_exceeded"
    assert res.terminated is True
    assert res.iterations == 0
    assert llm.calls == 0
    wall = trace.first("wall_time")
    assert wall["reason"] == "wall_time_budget_exceeded"
    assert wall["budget"] == pytest.approx(1e-9)


def test_wall_time_overrun_after_completion_overrides_reason(monkeypatch):
    """收尾时才超限：已完成的结果 reason 被覆盖为 wall_time_budget_exceeded（可观测不掩盖超时）。"""

    def _slow(**_kwargs):
        threading.Event().wait(0.12)
        return "slow ok"

    _register(monkeypatch, "w7_slow_tool", _slow)
    trace = _Recorder()
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_slow_tool", "arguments": {}}]])
    loop = AgentLoop(llm=llm, max_iterations=2, wall_time_budget=0.05, trace=trace)
    res = loop.run("q")
    assert "slow ok" in res.text
    assert res.reason == "wall_time_budget_exceeded"
    assert res.terminated is True
    assert trace.first("wall_time")["reason"] == "wall_time_budget_exceeded"


# ------------------------------------------------------------------ 用户停止


def test_request_stop_terminates_before_llm_call():
    """request_stop() 置位后下一轮即 user_stop，且不再调用 LLM。"""
    llm = _ScriptedLLM([[{"type": "text", "text": "unused"}]])
    loop = AgentLoop(llm=llm, trace=False)
    loop.request_stop()
    res = loop.run("q")
    assert res.reason == "user_stop"
    assert res.iterations == 0
    assert llm.calls == 0


def test_llm_should_stop_callback_terminates():
    """LLM 适配器暴露 should_stop 回调返回 True 时按 user_stop 收敛。"""

    class _LLM(_ScriptedLLM):
        def should_stop(self):
            return True

    llm = _LLM([[{"type": "text", "text": "unused"}]])
    res = AgentLoop(llm=llm, trace=False).run("q")
    assert res.reason == "user_stop"
    assert llm.calls == 0


def test_llm_should_stop_raising_is_ignored():
    """should_stop 回调抛异常时忽略该信号，主循环照常完成。"""

    class _LLM(_ScriptedLLM):
        def should_stop(self):
            raise RuntimeError("stop probe broken")

    llm = _LLM([[{"type": "text", "text": "answer"}], ])
    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert res.reason == "completed"
    assert res.text == "answer"


# ------------------------------------------------------------------ LLM 调用/重试


def test_llm_adapter_accepting_only_prompt_keyword_is_supported():
    """适配器只接受 prompt= 关键字时，第三形态回退仍能取到流。"""

    class _PromptOnlyLLM:
        def invoke(self, *, prompt):
            return [{"type": "text", "text": f"echo:{prompt}"}]

    res = AgentLoop(llm=_PromptOnlyLLM(), trace=False).run("hello")
    assert res.text == "echo:hello"
    assert res.reason == "completed"


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [(RuntimeError("llm boom"), "llm_error"), (TimeoutError("llm slow"), "llm_timeout")],
)
def test_llm_retry_exhausted_maps_to_reason_and_trace(exc, expected_reason):
    """重试耗尽后按异常类型归类 reason（llm_error / llm_timeout）并落 trace。"""
    trace = _Recorder()
    llm = _ScriptedLLM([exc])
    loop = AgentLoop(llm=llm, max_iterations=2, retry_policy=_Retry(max_attempts=2), trace=trace)
    res = loop.run("q")
    assert res.reason == expected_reason
    assert res.terminated is True
    assert f"[ERROR: {exc}]" in res.text
    assert expected_reason in trace.types()
    assert llm.calls == 2  # 用尽 max_attempts 才放弃


def test_retry_policy_internal_failure_falls_back_to_attempt_budget():
    """should_retry/sleep 自身报错时退化为"未超次数即重试"+固定退避，第二次成功。"""
    llm = _ScriptedLLM([RuntimeError("transient"), [{"type": "text", "text": "recovered"}]])
    policy = _Retry(max_attempts=3, should_retry_raises=True, sleep_raises=True)
    res = AgentLoop(llm=llm, max_iterations=2, retry_policy=policy, trace=False).run("q")
    assert res.text == "recovered"
    assert res.reason == "completed"
    assert llm.calls == 2


def test_stream_error_retry_rolls_back_partial_text_and_usage():
    """流中途可重试异常：回滚本轮 partial 文本与用量（避免重复拼接/双计费）并落 usage_rollback。"""
    stream_err = RuntimeError("stream broke")
    first = [
        {"type": "text", "text": "PARTIAL-", "usage_metadata": {"input_tokens": 100, "output_tokens": 50}},
        stream_err,
    ]
    llm = _ScriptedLLM([first, [{"type": "text", "text": "final answer"}]])
    trace = _Recorder()
    loop = AgentLoop(llm=llm, max_iterations=3, retry_policy=_Retry(max_attempts=3), trace=trace)
    res = loop.run("q")
    assert "PARTIAL-" not in res.text
    assert res.text == "final answer"
    assert res.reason == "completed"
    rollback = trace.first("usage_rollback")
    assert rollback["reverted_input"] == 100
    assert rollback["reverted_output"] == 50
    assert rollback["breaker_adjusted"] is False


@pytest.mark.parametrize(
    ("exc", "expected_reason", "expected_trace_type"),
    [
        (RuntimeError("stream boom"), "llm_error", "llm_stream_error"),
        (TimeoutError("stream slow"), "llm_timeout", "llm_timeout"),
    ],
)
def test_stream_error_without_retry_converges_to_terminal_reason(exc, expected_reason, expected_trace_type):
    """流异常且策略判定不可重试：保留已产出的部分文本并收敛 reason，不静默继续。"""
    trace = _Recorder()
    llm = _ScriptedLLM([[{"type": "text", "text": "head "}, exc]])
    loop = AgentLoop(llm=llm, max_iterations=3, retry_policy=_Retry(max_attempts=3, retry=False), trace=trace)
    res = loop.run("q")
    assert res.reason == expected_reason
    assert "head " in res.text
    assert f"[ERROR: {exc}]" in res.text
    event = trace.first(expected_trace_type)
    assert event["iteration"] == 1
    assert event["reason"] == expected_reason


# ------------------------------------------------------------------ chunk 形态归一化


def test_chunk_object_and_delta_forms_are_appended():
    """chunk 支持 dict(delta) / dict(type=None+text) / 任意对象（str() 化）三种形态。"""

    class _Chunk:
        def __str__(self):
            return "OBJ"

    chunks = [{"delta": "D"}, {"type": None, "text": "T"}, {"content": "C"}, "PLAINSTR", _Chunk()]
    res = AgentLoop(llm=_ScriptedLLM([chunks]), trace=False).run("q")
    assert "D" in res.text and "T" in res.text and "C" in res.text
    assert "PLAINSTR" in res.text and "OBJ" in res.text


def test_chunk_with_content_and_tool_calls_does_not_append_text(monkeypatch):
    """chunk 同时带 content 与 tool_calls 时 content 是工具名/参数载体，不得拼进输出。"""

    def _fn(**_kwargs):
        return "ok"

    _register(monkeypatch, "w7_content_tool", _fn)
    chunk = {"content": "IGNORED-CONTENT", "tool_calls": [{"name": "w7_content_tool", "arguments": {}}]}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, trace=False).run("q")
    assert "IGNORED-CONTENT" not in res.text
    assert "[tool w7_content_tool result] ok" in res.text


# ------------------------------------------------------------------ 畸形工具调用


def test_malformed_tool_calls_are_normalized_or_skipped(monkeypatch):
    """工具调用解析：字符串 tc / tool+args 键 / function 包裹 / 非 dict args / 无 name 丢弃。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w7_shape_tool", _fn)
    _register(monkeypatch, "w7_shape_tool2", _fn)
    chunks = [
        {"tool_calls": ["w7_shape_tool"]},
        {"tool_calls": {"name": "w7_shape_tool2", "arguments": {"k": 42}}},
        {"type": "tool_call", "tool": "w7_shape_tool2", "args": {"k": 1}},
        # OpenAI 原始形态：扁平 name + function 包裹参数
        {"type": "tool_call", "name": "w7_shape_tool", "function": {"name": "w7_shape_tool", "arguments": {"k": 2}}},
        {"type": "tool_call", "name": "w7_shape_tool", "arguments": 5},
        {"type": "tool_call", "arguments": {"k": 3}},  # 无工具名 -> 丢弃
    ]
    res = AgentLoop(llm=_ScriptedLLM([chunks]), max_iterations=2, trace=False).run("q")
    assert seen == [{}, {"k": 42}, {"k": 1}, {"k": 2}, {"value": 5}]
    assert "tool_not_found" not in res.text
    assert res.reason == "completed"


def test_concurrency_safe_probe_failure_degrades_to_serial(monkeypatch):
    """is_concurrency_safe 判定抛异常时按非安全处理（串行执行），工具仍被调用一次。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "serial ok"

    def _boom(_args):
        raise RuntimeError("safety probe broken")

    _register(monkeypatch, "w7_unsafe_probe_tool", _fn, safe=_boom)
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_unsafe_probe_tool", "arguments": {"a": 1}}]])
    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert seen == [{"a": 1}]
    assert "[tool w7_unsafe_probe_tool result] serial ok" in res.text
    assert res.reason == "completed"


def test_tool_keyboard_interrupt_propagates(monkeypatch):
    """工具内 KeyboardInterrupt 必须透传（不得被 tool_error 吞掉）。"""

    def _fn(**_kwargs):
        raise KeyboardInterrupt

    _register(monkeypatch, "w7_interrupt_tool", _fn)
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_interrupt_tool", "arguments": {}}]])
    with pytest.raises(KeyboardInterrupt):
        AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")


def test_concurrent_tool_keyboard_interrupt_propagates(monkeypatch):
    """并发（只读）工具内的 KeyboardInterrupt 同样透传，不被转成 tool_error。"""

    def _fn(**_kwargs):
        raise KeyboardInterrupt

    _register(monkeypatch, "w7_conc_interrupt_tool", _fn, safe=True)
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_conc_interrupt_tool", "arguments": {}}]])
    with pytest.raises(KeyboardInterrupt):
        AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")


def test_grounding_evidence_not_dict_yields_empty_authorized_snapshot(monkeypatch):
    """证据结构异常（非 dict）时授权快照退化为空 frozenset，未知工具被跳过，主循环仍完成。"""
    received = []

    def _fn(authorized=None, **_kwargs):
        received.append(authorized)
        return "ok"

    # 工具名刻意不含数字，避免工具名里的数字被 claim 抽取当作价格断言
    _register(monkeypatch, "wt_auth_tool", _fn)

    class _WeirdLedger:
        _evidence = ["not", "a", "dict"]

        def assert_price(self, symbol, price, authorized=None):
            return None

    chunks = [
        {
            "type": "tool_calls",
            "calls": [
                {"name": "wt_auth_tool", "arguments": {}},
                {"name": "wt_ghost_tool", "arguments": {}},
            ],
        }
    ]
    llm = _ScriptedLLM([chunks])
    res = AgentLoop(llm=llm, max_iterations=2, grounding=_WeirdLedger(), trace=False).run("q")
    assert received == [frozenset()]
    assert "tool_not_found: wt_ghost_tool" in res.text
    assert res.reason == "completed"
    assert res.grounding_verified is True


def test_grounding_extract_claims_failure_falls_back_to_regex(monkeypatch):
    """extract_claims 崩溃时回退旧正则提取数字，接地校验不因此放行/误杀。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    trace = _Recorder()

    def _boom(_text):
        raise RuntimeError("claim extractor broken")

    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", _boom)
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]])
    res = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=trace).run("q")
    assert res.reason == "completed"
    assert res.grounding_verified is True
    assert trace.first("grounding")["verified"] is True


def test_grounding_thousand_separated_price_is_normalized():
    """千分位价格（1,000.00）在证据内时被归一后校验通过（不做字符串直比）。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 1000.0}], provenance=_PROV)
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 1,000.00"}]])
    res = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False).run("q")
    assert res.reason == "completed"
    assert res.grounding_verified is True


# ------------------------------------------------------------------ 上下文压缩


@pytest.mark.parametrize("truncated", [True, False])
def test_context_compaction_records_trace_and_prefixes_banner(truncated):
    """上下文压缩被触发时落 trace；truncated 时 banner 前置到输出，非截断则只记 trace。"""
    banner = "TRUNCATED: context folded"
    ctx = _FakeContext(
        messages=[{"role": "user", "content": "x" * 600}],
        compact_result=CompactResult(truncated=truncated, banner=banner, text="folded"),
    )
    trace = _Recorder()
    llm = _ScriptedLLM([[{"type": "text", "text": "answer"}]])
    res = AgentLoop(llm=llm, max_iterations=2, token_limit=200, context_manager=ctx, trace=trace).run("q")
    assert ctx.compact_calls == 1
    event = trace.first("context_compact")
    assert event["truncated"] is truncated
    if truncated:
        assert event["banner"] == banner
        assert res.text.startswith(f"[{banner}]")
        assert res.text.endswith("answer")
    else:
        assert res.text == "answer"


def test_context_compact_failure_does_not_abort_loop():
    """context_manager.compact 抛异常时记 context_error 并继续（压缩是优化不是正确性前提）。"""
    ctx = _FakeContext(messages=[{"role": "user", "content": "y" * 600}], compact_raises=RuntimeError("fold broken"))
    trace = _Recorder()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]),
        max_iterations=2,
        token_limit=200,
        context_manager=ctx,
        trace=trace,
    ).run("q")
    assert res.reason == "completed"
    assert res.text == "answer"
    assert trace.first("context_error")["error"] == "fold broken"


# ------------------------------------------------------------------ 预算熔断


def test_budget_should_fallback_failure_fails_closed():
    """熔断判定自身抛异常时 fail-closed（budget_fallback），并落 budget_error trace。"""

    class _Breaker:
        def should_fallback(self, cost=0.0):
            raise RuntimeError("breaker broken")

    trace = _Recorder()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
        max_iterations=2,
        budget_breaker=_Breaker(),
        trace=trace,
    ).run("q")
    assert res.reason == "budget_fallback"
    assert res.terminated is True
    assert "breaker broken" in trace.first("budget_error")["error"]


def test_budget_breaker_without_should_fallback_does_not_break_loop():
    """熔断器未实现 should_fallback 时按"不熔断"处理（仅累计用量），主循环正常完成。"""
    recorded = []

    class _Breaker:
        def record_usage(self, usage):
            recorded.append(usage)

    res = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), budget_breaker=_Breaker(), trace=False).run("q")
    assert res.reason == "completed"
    assert recorded == []


def test_budget_without_estimate_cost_uses_env_pricing_and_records_usage(monkeypatch):
    """熔断器无 estimate_cost 时按环境变量单价计价，并把真实 usage 记入 record_usage。"""
    monkeypatch.setenv("HERO_LLM_PRICE_IN", "0.15")
    monkeypatch.setenv("HERO_LLM_PRICE_OUT", "0.60")
    recorded = []

    class _Breaker:
        def record_usage(self, usage):
            recorded.append(usage)

    chunk = {"type": "text", "text": "hi", "usage_metadata": {"input_tokens": 100, "output_tokens": 50}}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, budget_breaker=_Breaker(), trace=False).run("q")
    assert res.reason == "completed"
    assert recorded == [{"input_tokens": 100, "output_tokens": 50}]


# ------------------------------------------------------------------ token 上限三处熔断


def test_token_limit_mid_stream_truncates_and_traces(tmp_path):
    """流中越过 token 上限时立即截断并附加 TRUNCATED 标识，同时落 trace。"""
    trace = _Recorder(str(tmp_path / "t.jsonl"))
    llm = _ScriptedLLM([[{"type": "text", "text": "z" * 400}]])
    res = AgentLoop(llm=llm, max_iterations=3, token_limit=10, trace=trace).run("q")
    assert res.reason == "token_limit"
    assert res.terminated is True
    assert "[TRUNCATED: token_limit exceeded]" in res.text
    assert len(res.text) < 400
    assert trace.first("truncated")["banner"] == "TRUNCATED: token_limit exceeded"


def test_token_limit_after_tool_output_truncates_iteration(tmp_path, monkeypatch):
    """工具输出使 buffer 膨胀越过上限时，在迭代末尾熔断（避免工具结果绕过限制）。"""

    def _big(**_kwargs):
        return "R" * 4000

    _register(monkeypatch, "w7_big_tool", _big)
    trace = _Recorder(str(tmp_path / "t.jsonl"))
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_big_tool", "arguments": {}}]])
    res = AgentLoop(llm=llm, max_iterations=3, token_limit=50, trace=trace).run("q")
    assert res.reason == "token_limit"
    assert res.terminated is True
    assert "[TRUNCATED: token_limit exceeded]" in res.text
    assert len(res.text) < 4000
    truncated = trace.first("truncated")
    assert truncated["iteration"] == 1
    assert truncated["reason"] == "token_limit"


def test_token_limit_from_context_before_iteration_truncates(tmp_path):
    """上下文膨胀（而非 buffer）也能在轮次开始前触发 token_limit 熔断。"""
    ctx = _FakeContext(messages=[{"role": "user", "content": "c" * 4000}])
    trace = _Recorder(str(tmp_path / "t.jsonl"))
    llm = _ScriptedLLM([[{"type": "text", "text": "unused"}]])
    res = AgentLoop(llm=llm, max_iterations=2, token_limit=100, context_manager=ctx, trace=trace).run("q")
    assert res.reason == "token_limit"
    assert llm.calls == 0
    assert "[TRUNCATED: token_limit exceeded]" in res.text
    assert trace.first("truncated")["reason"] == "token_limit"


# ------------------------------------------------------------------ 记忆召回/回写


def test_memory_recall_failure_warns_and_continues(caplog):
    """记忆召回抛异常：告警后按空命中继续（召回是增强项，不得阻断主循环）。"""

    class _Store:
        def recall(self, query):
            raise RuntimeError("recall broken")

    llm = _ScriptedLLM([[{"type": "text", "text": "answer"}], ])
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        res = AgentLoop(llm=llm, max_iterations=2, memory_store=_Store(), trace=False).run("q")
    assert res.reason == "completed"
    assert res.text == "answer"
    assert "memory recall failed" in caplog.text


def test_memory_recall_top_k_signature_is_retried():
    """recall 只接受 (query, top_k) 时用 top_k=5 重试，并把命中注入上下文。"""
    ctx = _FakeContext()

    class _Store:
        def __init__(self):
            self.calls = []

        def recall(self, query, top_k=None):
            self.calls.append(top_k)
            if top_k is None:
                raise TypeError("top_k required")
            return [{"content": "AAPL close 100.0"}]

    store = _Store()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]),
        max_iterations=2,
        memory_store=store,
        context_manager=ctx,
        trace=False,
    ).run("q")
    assert store.calls == [None, 5]
    assert ("system", "[memory recall] AAPL close 100.0") in ctx.added
    assert res.reason == "completed"


def test_memory_store_search_fallback_and_text_field():
    """MemoryStore 只提供 search 时用它召回，命中 dict 的 text 字段被注入上下文。"""
    ctx = _FakeContext()

    class _Store:
        def search(self, query):
            return [{"text": "previous session note"}]

    AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]),
        max_iterations=2,
        memory_store=_Store(),
        context_manager=ctx,
        trace=False,
    ).run("q")
    assert ("system", "[memory recall] previous session note") in ctx.added


def test_memory_context_inject_failure_warns(caplog):
    """召回命中写上下文失败时告警且不中断（上下文注入是 best-effort）。"""
    ctx = _FakeContext(add_raises=True)

    class _Store:
        def recall(self, query):
            return [{"content": "note"}]

    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        res = AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]),
            max_iterations=2,
            memory_store=_Store(),
            context_manager=ctx,
            trace=False,
        ).run("q")
    assert res.reason == "completed"
    assert "memory recall context inject failed" in caplog.text


def test_memory_writeback_failure_does_not_break_run():
    """结束回写记忆失败被吞掉（write 抛异常不影响结果）。"""
    calls = []

    class _Store:
        def write(self, key, content):
            calls.append((key, content))
            raise RuntimeError("write broken")

    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "final answer"}]]),
        memory_store=_Store(),
        trace=False,
    ).run("q")
    assert res.reason == "completed"
    assert res.text == "final answer"
    assert len(calls) == 1
    assert calls[0][0].startswith("loop:")
    assert calls[0][1] == "final answer"


# ------------------------------------------------------------------ trace 故障隔离


def test_broken_trace_writer_does_not_abort_run(monkeypatch, tmp_path):
    """轨迹写入全链路失败时主循环照常完成（trace 是旁路，不得反噬业务）。"""

    def _fn(**_kwargs):
        return "tool ok"

    _register(monkeypatch, "w7_traced_tool", _fn)

    class _Store:
        def recall(self, query):
            return []

    writer = _BrokenWriter(str(tmp_path / "broken.jsonl"))
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_traced_tool", "arguments": {}}]])
    res = AgentLoop(llm=llm, max_iterations=2, memory_store=_Store(), trace=writer).run("q")
    assert res.reason == "completed"
    assert "[tool w7_traced_tool result] tool ok" in res.text
    assert writer.attempts > 0
    # 仍按鸭子类型 writer 暴露路径
    assert res.trace_path == str(tmp_path / "broken.jsonl")


def test_estimate_tokens_is_used_for_token_counting():
    """token_count 与文本估算口径一致（4 字符/token），保证预算判定可复现。"""
    text = "12345678" * 5
    res = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": text}]]), max_iterations=1, trace=False).run("q")
    assert res.token_count == estimate_tokens(text)
    assert res.token_count == len(text) // 4


def test_stream_keyboard_interrupt_propagates():
    """流迭代阶段 KeyboardInterrupt 必须透传（不得被 llm_error 收敛）。"""
    llm = _ScriptedLLM([[{"type": "text", "text": "head"}, KeyboardInterrupt()]])
    with pytest.raises(KeyboardInterrupt):
        AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")


def test_stream_error_with_broken_retry_probe_is_terminal():
    """流异常时 should_retry 自身报错：按不可重试处理（单次调用即收敛 llm_error）。"""
    llm = _ScriptedLLM([[RuntimeError("stream boom")]])
    policy = _Retry(max_attempts=3, should_retry_raises=True)
    res = AgentLoop(llm=llm, max_iterations=3, retry_policy=policy, trace=False).run("q")
    assert res.reason == "llm_error"
    assert "[ERROR: stream boom]" in res.text
    assert llm.calls == 1
