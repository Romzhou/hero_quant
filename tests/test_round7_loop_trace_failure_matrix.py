"""Round7 X4：agent/loop.py 的「旁路故障隔离」矩阵 + 配置/兼容分支收尾。

主题：trace 落盘、告警、指标、用量侧车、skills 摘要、接地校验取证都是旁路/增强项，
它们失败时**必须**保持业务结论（reason/文本/截断标记/接地结论）不变。

覆盖目标（src/hero_quant/agent/loop.py）：
- trace 写入全坏时，各条终止路径的结论不变：壁时间熔断、三处 token 截断、预算熔断、
  LLM 错误、图错误、上下文压缩、接地校验、收尾壁时间覆盖
- trace writer 初始化失败只告警不阻断（默认回灌 / 显式路径两种入口），回放路径仍能给出 trace_path
- HERO_ALLOW_TMP_REPLAY 环境闸门：仅在显式开启时接受临时目录回放件
- replay= 字符串别名；HERO_WALL_TIME_BUDGET 脏值/Settings 失败/归一失败均退化为不限时
- skills_digest 必须真正进入 system prompt（无参签名补偿）+ 注入失败只告警
- 接地校验：证据读取异常按"无 symbol"处理、claims 的 list/原始串形态、
  字符串价格需 float 归一二试、文本未提 symbol 时回退首个 symbol（主循环与图路径各一套）
- 上下文压缩阶段中断信号透传
- LLM 适配器三种签名全不匹配 -> llm_error（而非抛 AttributeError 崩主循环）
- 用量侧车写入失败不阻断；上下文 token 估算异常视为 0

断言均落在具体 reason/文本/事件/调用参数上。
"""

from __future__ import annotations

import logging
import threading

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


class _BrokenWriter:
    """trace 写入恒失败（path 指向临时目录，避免侧车落到仓库根）。"""

    def __init__(self, path):
        self.path = path
        self.attempts = 0

    def append(self, obj):
        self.attempts += 1
        raise OSError("trace disk full")


class _Recorder:
    def __init__(self, path):
        self.path = path
        self.events = []

    def append(self, obj):
        self.events.append(obj)

    def first(self, type_name):
        for e in self.events:
            if e.get("type") == type_name:
                return e
        return None


class _Ctx:
    """最小上下文：可配置 compact 结果/异常；add 可失败。"""

    def __init__(self, messages=None, compact_result=None, compact_raises=None, add_raises=False, prompt=None):
        self._messages = list(messages or [])
        self._compact_result = compact_result
        self._compact_raises = compact_raises
        self._add_raises = add_raises
        self._prompt = prompt
        self.added = []

    def add(self, role, content):
        if self._add_raises:
            raise RuntimeError("context add broken")
        self.added.append((role, content))

    def compact(self):
        if self._compact_raises is not None:
            raise self._compact_raises
        return self._compact_result


class _Compact:
    def __init__(self, truncated, banner="TRUNCATED: context folded"):
        self.truncated = truncated
        self.banner = banner
        self.text = "folded"


def _broken_loop(tmp_path, llm, **kwargs):
    return AgentLoop(llm=llm, trace=_BrokenWriter(str(tmp_path / "trace.jsonl")), **kwargs)


# ------------------------------------------------------------------ trace 故障矩阵


def test_trace_failure_keeps_wall_budget_verdict(tmp_path):
    """trace 全坏时壁时间熔断照旧生效（reason 与预算值不受影响）。"""
    res = _broken_loop(tmp_path, _ScriptedLLM([[{"type": "text", "text": "x"}]]), wall_time_budget=1e-9).run("q")
    assert res.reason == "wall_time_budget_exceeded"
    assert res.iterations == 0


def test_trace_failure_keeps_budget_fallback_verdict(tmp_path):
    """trace 全坏时预算熔断照旧生效。"""

    class _Breaker:
        def should_fallback(self, cost=0.0):
            return True

    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "text", "text": "x"}]]), budget_breaker=_Breaker()
    ).run("q")
    assert res.reason == "budget_fallback"
    assert res.terminated is True


def test_trace_failure_keeps_llm_error_verdict(tmp_path):
    """trace 全坏时 LLM 失败仍收敛为 llm_error 并保留错误文本。"""
    res = _broken_loop(
        tmp_path, _ScriptedLLM([RuntimeError("llm down")]), retry_policy=_NoRetry()
    ).run("q")
    assert res.reason == "llm_error"
    assert "[ERROR: llm down]" in res.text


def test_trace_failure_keeps_midstream_token_truncation(tmp_path):
    """trace 全坏时流中 token 截断标记仍然写入输出。"""
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "text", "text": "z" * 400}]]), token_limit=10
    ).run("q")
    assert res.reason == "token_limit"
    assert "[TRUNCATED: token_limit exceeded]" in res.text


def test_trace_failure_keeps_context_based_token_truncation(tmp_path):
    """trace 全坏时"上下文膨胀"触发的轮次前截断仍然生效。"""
    ctx = _Ctx(messages=[{"role": "user", "content": "c" * 4000}])
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "text", "text": "unused"}]]), token_limit=100, context_manager=ctx
    ).run("q")
    assert res.reason == "token_limit"
    assert "[TRUNCATED: token_limit exceeded]" in res.text


def test_trace_failure_keeps_tool_output_token_truncation(tmp_path, monkeypatch):
    """trace 全坏时工具输出膨胀触发的迭代末截断仍然生效。"""

    def _big(**_kwargs):
        return "R" * 4000

    _register(monkeypatch, "wt_big_tool", _big)
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "tool_call", "name": "wt_big_tool", "arguments": {}}]]), token_limit=50
    ).run("q")
    assert res.reason == "token_limit"
    assert "[TRUNCATED: token_limit exceeded]" in res.text


def test_trace_failure_keeps_compaction_and_grounding_verdict(tmp_path):
    """trace 全坏时上下文压缩仍前置 banner，接地结论仍按证据判定。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    ctx = _Ctx(messages=[{"role": "user", "content": "x" * 600}], compact_result=_Compact(truncated=True))
    res = _broken_loop(
        tmp_path,
        _ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]]),
        token_limit=200,
        context_manager=ctx,
        grounding=ledger,
    ).run("q")
    assert res.reason == "completed"
    assert res.grounding_verified is True
    assert res.text.startswith("[TRUNCATED: context folded]")


def test_trace_failure_keeps_compaction_error_verdict(tmp_path):
    """trace 全坏 + 压缩抛异常：仍然只告警，业务结论 completed。"""
    ctx = _Ctx(messages=[{"role": "user", "content": "y" * 600}], compact_raises=RuntimeError("fold broken"))
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "text", "text": "answer"}]]), token_limit=200, context_manager=ctx
    ).run("q")
    assert res.reason == "completed"
    assert res.text == "answer"


def test_trace_failure_keeps_graph_error_verdict(tmp_path):
    """trace 全坏时图异常仍收敛为 graph_error。"""

    class _BadGraph:
        def invoke(self, state):
            raise RuntimeError("graph boom")

    res = _broken_loop(tmp_path, _ScriptedLLM([[]]), use_graph=True, graph=_BadGraph()).run("q")
    assert res.reason == "graph_error"
    assert res.text == "graph_error: graph boom"


def test_trace_failure_keeps_wall_time_override_verdict(tmp_path, monkeypatch):
    """trace 全坏时"收尾超限覆盖 reason"仍然生效。"""

    def _slow(**_kwargs):
        threading.Event().wait(0.12)
        return "slow ok"

    _register(monkeypatch, "wt_slow_tool", _slow)
    res = _broken_loop(
        tmp_path,
        _ScriptedLLM([[{"type": "tool_call", "name": "wt_slow_tool", "arguments": {}}]]),
        wall_time_budget=0.05,
    ).run("q")
    assert res.reason == "wall_time_budget_exceeded"
    assert "slow ok" in res.text


class _NoRetry:
    """不可重试策略桩。"""

    max_attempts = 3

    def should_retry(self, exc, attempt):
        return False

    def sleep(self, attempt):
        return None


def _register(monkeypatch, name, func, *, safe=False):
    from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

    spec = ToolSpec(
        name=name,
        description=f"round7 tool {name}",
        func=func,
        is_concurrency_safe=lambda args: safe,
    )
    monkeypatch.setitem(TOOL_REGISTRY, name, spec)


# ------------------------------------------------------------------ trace writer 初始化


def test_trace_true_uses_default_sink(tmp_path, monkeypatch):
    """trace=True 与 None 同走默认回灌（默认目录 + trace.jsonl）。"""
    monkeypatch.setenv("HERO_TRACE_DIR", str(tmp_path))
    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), max_iterations=2, trace=True)
    res = loop.run("q")
    assert res.reason == "completed"
    assert res.trace_path == str(tmp_path / "trace.jsonl")
    assert (tmp_path / "trace.jsonl").exists()


def test_trace_writer_init_failure_is_warn_only(tmp_path, monkeypatch):
    """显式 trace 路径构造 writer 失败：降级为无 trace，主循环照常完成。"""

    def _boom(*a, **kw):
        raise OSError("trace dir unwritable")

    monkeypatch.setattr("hero_quant.agent.trace.TraceWriter", _boom)
    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), max_iterations=2, trace=str(tmp_path / "t.jsonl"))
    assert loop._ensure_trace_writer() is None
    assert loop.run("q").text == "hi"


def test_default_trace_writer_failure_is_warn_only(tmp_path, monkeypatch):
    """默认回灌构造失败（trace=None）：只告警，不阻断主循环。"""
    monkeypatch.setenv("HERO_TRACE_DIR", str(tmp_path))

    def _boom(*a, **kw):
        raise OSError("default trace dir unwritable")

    monkeypatch.setattr("hero_quant.agent.trace.TraceWriter", _boom)
    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), max_iterations=2, trace=None)
    assert loop._ensure_trace_writer() is None
    assert loop.run("q").text == "hi"


def test_replay_with_failed_trace_writer_keeps_str_trace_path(tmp_path, monkeypatch):
    """writer 构造失败但 trace 是路径字符串时，回放结果仍暴露该路径（不误报 None）。"""

    def _boom(*a, **kw):
        raise OSError("trace dir unwritable")

    replay = tmp_path / "replay.json"
    replay.write_text('{"text": "replayed"}', encoding="utf-8")
    monkeypatch.setattr("hero_quant.agent.trace.TraceWriter", _boom)
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(tmp_path),
        trace=str(tmp_path / "trace.jsonl"),
    )
    res = loop.run("q")
    assert res.text == "replayed"
    assert res.trace_path == str(tmp_path / "trace.jsonl")


# ------------------------------------------------------------------ 回放闸门与别名


def test_replay_tmp_dir_requires_explicit_env(tmp_path, monkeypatch):
    """回放件位于系统临时目录：仅在 HERO_ALLOW_TMP_REPLAY=1 时被接受。"""
    replay = tmp_path / "replay.json"
    replay.write_text('{"text": "tmp replay"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="outside allowed directory"):
        AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), trace=False)

    monkeypatch.setenv("HERO_ALLOW_TMP_REPLAY", "1")
    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), trace=False)
    assert loop.run("q").text == "tmp replay"


def test_replay_flag_string_alias_sets_replay_path(tmp_path):
    """replay=<路径> 字符串别名等价于 replay_path。"""
    replay = tmp_path / "replay.json"
    replay.write_text('{"text": "flag alias"}', encoding="utf-8")
    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay=str(replay), allow_root=str(tmp_path), trace=False)
    assert loop.run("q").text == "flag alias"
    assert str(loop.replay_path) == str(replay.resolve())


# ------------------------------------------------------------------ 壁时间预算配置


def test_dirty_wall_time_env_value_degrades_to_unlimited(monkeypatch, caplog):
    """HERO_WALL_TIME_BUDGET 非数值：告警并退化为不限时（不抛）。"""
    monkeypatch.delenv("HERO_WALL_TIME_BUDGET_SECONDS", raising=False)
    monkeypatch.setenv("HERO_WALL_TIME_BUDGET", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        loop = AgentLoop(llm=_ScriptedLLM([[]]), trace=False)
    assert loop.wall_time_budget is None
    assert "wall_time env parse failed" in caplog.text


def test_wall_time_settings_failure_degrades_to_unlimited(monkeypatch):
    """Settings 读取失败时退化为不限时（告警而非崩溃）。"""
    monkeypatch.delenv("HERO_WALL_TIME_BUDGET", raising=False)
    monkeypatch.delenv("HERO_WALL_TIME_BUDGET_SECONDS", raising=False)

    def _boom(*a, **kw):
        raise RuntimeError("settings broken")

    monkeypatch.setattr("hero_quant.config.settings.Settings", _boom)
    loop = AgentLoop(llm=_ScriptedLLM([[]]), trace=False)
    assert loop.wall_time_budget is None


def test_invalid_wall_time_budget_value_degrades_to_unlimited(caplog):
    """显式传入非法 wall_time_budget：归一失败告警，按不限时处理。"""
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        loop = AgentLoop(llm=_ScriptedLLM([[]]), wall_time_budget="abc", trace=False)
    assert loop.wall_time_budget is None
    assert "wall_time budget normalize failed" in caplog.text


# ------------------------------------------------------------------ skills 摘要


class _Skills:
    def __init__(self, text="skill-alpha: does alpha"):
        self._text = text

    def get_descriptions(self):
        return self._text


class _StoreEmpty:
    def recall(self, query):
        return []


def test_skills_digest_is_compensated_into_prompt(tmp_path):
    """build_system_prompt 丢掉 digest 时，通过 add 补偿写入（Wave5 加固语义）。"""

    class _CtxWithPrompt(_Ctx):
        def build_system_prompt(self, skills_digest=None):
            return "system prompt without digest"

    ctx = _CtxWithPrompt()
    AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
        max_iterations=1,
        context_manager=ctx,
        memory_store=_StoreEmpty(),
        skills_loader=_Skills(),
        trace=False,
    ).run("q")
    digests = [c for role, c in ctx.added if c.startswith("[skills_digest]")]
    assert digests[0] == "[skills_digest] skill-alpha: does alpha"
    assert len(digests) == 2  # 先注入，再因 prompt 缺失而补偿一次


def test_skills_digest_supports_prompt_builder_without_kwargs(tmp_path):
    """build_system_prompt 只接受无参签名时，用无参调用校验并在缺失时补偿。"""

    class _CtxNoKwargs(_Ctx):
        def build_system_prompt(self):
            return "prompt without digest"

    ctx = _CtxNoKwargs()
    AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
        max_iterations=1,
        context_manager=ctx,
        memory_store=_StoreEmpty(),
        skills_loader=_Skills(),
        trace=False,
    ).run("q")
    digests = [c for role, c in ctx.added if c.startswith("[skills_digest]")]
    assert len(digests) == 2


def test_skills_digest_inject_failure_is_warned(caplog):
    """skills_digest 写上下文失败只记 debug 日志，不影响主循环。"""
    ctx = _Ctx(add_raises=True)
    with caplog.at_level(logging.DEBUG, logger="hero_quant.agent.loop"):
        res = AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
            max_iterations=1,
            context_manager=ctx,
            memory_store=_StoreEmpty(),
            skills_loader=_Skills(),
            trace=False,
        ).run("q")
    assert res.reason == "completed"
    assert "skills_digest inject failed" in caplog.text


# ------------------------------------------------------------------ 接地校验细节（主循环）


class _StrStrictLedger:
    """只接受数值价格：字符串价格视为非法，用于验证 float 归一二试。"""

    def __init__(self, price=100.0):
        self._evidence = {"AAPL": {"closes": {price}}}
        self.ok = []

    def assert_price(self, symbol, price, authorized=None):
        if isinstance(price, str):
            raise TypeError("string price not accepted")
        if abs(float(price) - 100.0) > 1e-9:
            raise AssertionError("price out of evidence")
        self.ok.append((symbol, float(price)))


def test_grounding_evidence_access_failure_treated_as_no_symbols():
    """读取证据结构抛异常时按"无 symbol"处理（不误判为幻觉，也不放行越界价格）。"""

    class _RaisingLedger:
        def __init__(self):
            self.calls = 0

        @property
        def _evidence(self):
            raise RuntimeError("evidence store broken")

        def assert_price(self, symbol, price, authorized=None):
            self.calls += 1
            raise AssertionError("no symbol should be verified")

    ledger = _RaisingLedger()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "AAPL looks fine"}]]),
        max_iterations=2,
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.reason == "completed"
    assert res.grounding_verified is True
    assert ledger.calls == 0


@pytest.mark.parametrize(
    "claim",
    [
        {"type": "price", "value": ["100.0"]},   # list 形态
        {"type": "price", "value": "about 100.0"},  # 非数值 -> 回退 raw 正则
    ],
)
def test_grounding_claims_value_forms_are_supported(monkeypatch, claim):
    """claims 的 value 可以是 list 或非数值（回退 raw 里抽数字），都要参与校验。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", lambda _t: [dict(claim)])
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]]),
        max_iterations=2,
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.reason == "completed"
    assert res.grounding_verified is True


def test_grounding_string_price_needs_float_second_try():
    """价格以字符串形态进入校验时，第一次失败后应 float 归一重试并成功。"""
    ledger = _StrStrictLedger()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]]),
        max_iterations=2,
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.reason == "completed"
    assert res.grounding_verified is True
    assert ledger.ok == [("AAPL", 100.0)]


def test_grounding_falls_back_to_first_symbol_when_text_lacks_symbol():
    """文本只有数字没有 symbol 时，回退用首个证据 symbol 做 float 归一校验。"""
    ledger = _StrStrictLedger()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "close 100.0"}]]),
        max_iterations=2,
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.grounding_verified is True
    assert ledger.ok == [("AAPL", 100.0)]


# ------------------------------------------------------------------ 接地校验细节（图路径）


class _GraphResult:
    def __init__(self, text):
        self._text = text

    def invoke(self, state, config=None):
        return {"messages": [{"content": self._text}]}


def test_graph_grounding_string_price_needs_float_second_try():
    """图路径同样对字符串价格做 float 归一二试。"""
    ledger = _StrStrictLedger()
    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_GraphResult("AAPL close 100.0"), grounding=ledger, trace=False
    ).run("q")
    assert res.grounding_verified is True
    assert ledger.ok == [("AAPL", 100.0)]


def test_graph_grounding_falls_back_to_first_symbol():
    """图路径文本无 symbol 时回退首个证据 symbol 并 float 归一。"""
    ledger = _StrStrictLedger()
    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_GraphResult("close 100.0"), grounding=ledger, trace=False
    ).run("q")
    assert res.grounding_verified is True
    assert ledger.ok == [("AAPL", 100.0)]


def test_graph_grounding_regex_fallback_without_numbers_passes(monkeypatch):
    """图路径 extract_claims 崩溃且文本无数字 -> 回退正则后视为无幻觉。"""

    def _boom(_text):
        raise RuntimeError("claim extractor broken")

    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", _boom)
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    res = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_GraphResult("AAPL looks fine"),
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.grounding_verified is True


# ------------------------------------------------------------------ 其它旁路故障


def test_compact_keyboard_interrupt_propagates():
    """上下文压缩阶段 KeyboardInterrupt 必须透传（不得被 context_error 吞掉）。"""
    ctx = _Ctx(messages=[{"role": "user", "content": "y" * 600}], compact_raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
            max_iterations=1,
            token_limit=200,
            context_manager=ctx,
            trace=False,
        ).run("q")


def test_llm_signature_mismatch_surfaces_as_llm_error():
    """适配器三种调用形态都签名不匹配 -> llm_error（而不是抛 AttributeError 崩主循环）。"""

    class _PickLLM:
        def invoke(self):
            return "unreachable"

    res = AgentLoop(llm=_PickLLM(), max_iterations=1, retry_policy=_NoRetry(), trace=False).run("q")
    assert res.reason == "llm_error"
    assert "unexpected keyword argument 'prompt'" in res.text


def test_usage_sidecar_write_failure_does_not_break_run(tmp_path):
    """用量侧车写盘失败（目录不可创建）被吞掉，主循环结论不变。"""
    writer = _Recorder(str(tmp_path / "proc" / "forbidden" / "trace.jsonl"))
    (tmp_path / "proc").write_text("not a directory", encoding="utf-8")
    chunk = {"type": "text", "text": "final", "usage_metadata": {"input_tokens": 3, "output_tokens": 4}}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, trace=writer).run("q")
    assert res.reason == "completed"
    assert res.text == "final"
    assert writer.first("llm_usage")["llm_usage"] == {"input_tokens": 3, "output_tokens": 4}


def test_context_token_estimation_failure_is_zero():
    """上下文 token 估算抛异常时按 0 计（不阻断主循环）。"""

    class _BadCtx:
        """_messages 读取即抛异常的上下文实现。"""

        added: list = []

        @property
        def _messages(self):
            raise RuntimeError("messages broken")

        def add(self, role, content):
            self.added.append((role, content))

        def compact(self):
            raise AssertionError("compact should not be reached")

    ctx = _BadCtx()
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
        max_iterations=2,
        token_limit=100,
        context_manager=ctx,
        trace=False,
    ).run("q")
    assert res.reason == "completed"
    assert res.text == "hi"


# ------------------------------------------------------------------ trace 故障矩阵（续）


def test_trace_failure_keeps_stream_retry_rollback(tmp_path):
    """trace 全坏时流异常重试仍回滚 partial 文本（不重复拼接），只保留最终答案。"""
    trace = _BrokenWriter(str(tmp_path / "trace.jsonl"))
    script = [
        [
            {"type": "text", "text": "PARTIAL-", "usage_metadata": {"input_tokens": 10, "output_tokens": 4}},
            RuntimeError("stream broke"),
        ],
        [{"type": "text", "text": "final answer"}],
    ]
    llm = _ScriptedLLM(script)
    res = AgentLoop(llm=llm, max_iterations=3, retry_policy=_Retryable(), trace=trace).run("q")
    assert res.text == "final answer"
    assert res.reason == "completed"
    assert llm.calls == 2


def test_trace_failure_keeps_tool_timeout_verdict(tmp_path, monkeypatch):
    """trace 全坏时工具超时仍转为 tool_error 文本（不丢失超时语义）。"""

    def _slow(**_kwargs):
        threading.Event().wait(0.2)
        return "late"

    _register(monkeypatch, "wt_slow_timeout_tool", _slow, safe=True)
    from hero_quant.tools.registry import TOOL_REGISTRY

    TOOL_REGISTRY["wt_slow_timeout_tool"].timeoutMs = 20
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "tool_call", "name": "wt_slow_timeout_tool", "arguments": {}}]])
    ).run("q")
    assert "tool_error: timeout after 20ms" in res.text


def test_trace_failure_keeps_bad_json_args_degrade(tmp_path, monkeypatch):
    """trace 全坏时坏 JSON 参数仍降级为空 dict 并执行工具。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "wt_bad_json_tool", _fn)
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "tool_call", "name": "wt_bad_json_tool", "arguments": "{not-json"}]])
    ).run("q")
    assert seen == [{}]
    assert res.reason == "completed"


def test_trace_failure_keeps_skills_digest_injection(tmp_path):
    """trace 全坏时 skills_digest 仍被注入上下文。"""
    ctx = _Ctx()
    AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
        max_iterations=1,
        context_manager=ctx,
        memory_store=_StoreEmpty(),
        skills_loader=_Skills(),
        trace=_BrokenWriter(str(tmp_path / "trace.jsonl")),
    ).run("q")
    assert any(c.startswith("[skills_digest]") for role, c in ctx.added)


def test_trace_failure_keeps_memory_writeback(tmp_path):
    """trace 全坏时结束记忆回写仍然发生（回写不依赖 trace）。"""
    written = []

    class _Store:
        def recall(self, query):
            return []

        def write(self, key, content):
            written.append((key, content))

    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "final answer"}]]),
        memory_store=_Store(),
        trace=_BrokenWriter(str(tmp_path / "trace.jsonl")),
    ).run("q")
    assert res.reason == "completed"
    assert written and written[0][1] == "final answer"


def test_trace_failure_keeps_replay_result(tmp_path):
    """trace 全坏时回放短路仍返回记录文本。"""
    replay = tmp_path / "replay.json"
    replay.write_text('{"llm_usage": {"input_tokens": 4, "output_tokens": 2}, "text": "replayed"}', encoding="utf-8")
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(tmp_path),
        trace=_BrokenWriter(str(tmp_path / "traces" / "trace.jsonl")),
    )
    assert loop.run("q").text == "replayed"


def test_trace_failure_keeps_non_truncated_compaction(tmp_path):
    """trace 全坏时"未截断的压缩"不改写输出文本。"""
    ctx = _Ctx(messages=[{"role": "user", "content": "x" * 600}], compact_result=_Compact(truncated=False))
    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "text", "text": "answer"}]]), token_limit=200, context_manager=ctx
    ).run("q")
    assert res.text == "answer"
    assert res.reason == "completed"


def test_trace_failure_keeps_budget_error_fail_closed(tmp_path):
    """trace 全坏时熔断判定异常仍然 fail-closed。"""

    class _Breaker:
        def should_fallback(self, cost=0.0):
            raise RuntimeError("breaker broken")

    res = _broken_loop(
        tmp_path, _ScriptedLLM([[{"type": "text", "text": "hi"}]]), budget_breaker=_Breaker()
    ).run("q")
    assert res.reason == "budget_fallback"


def test_trace_failure_keeps_usage_accounting(tmp_path):
    """trace 全坏时累计用量仍写入 LoopResult.token_count（不丢计数）。"""
    chunk = {"type": "text", "text": "final", "usage_metadata": {"input_tokens": 3, "output_tokens": 4}}
    res = _broken_loop(tmp_path, _ScriptedLLM([[chunk]])).run("q")
    assert res.reason == "completed"
    assert res.text == "final"
    assert res.token_count == 1


def test_failed_trace_writer_with_str_trace_still_publishes_usage_sidecar(tmp_path, monkeypatch):
    """writer 构造失败但 trace 是路径字符串：用量侧车按该路径目录落盘。"""
    target = tmp_path / "traces" / "trace.jsonl"

    def _boom(*a, **kw):
        raise OSError("trace dir unwritable")

    monkeypatch.setattr("hero_quant.agent.trace.TraceWriter", _boom)
    chunk = {"type": "text", "text": "hi", "usage_metadata": {"input_tokens": 2, "output_tokens": 1}}
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, trace=str(target)).run("q")
    assert res.reason == "completed"
    assert res.trace_path == str(target)  # writer 不可用时回落到调用方给的路径
    import json

    payload = json.loads((target.parent / "llm_usage.json").read_text(encoding="utf-8"))
    assert payload["llm_usage"] == {"input_tokens": 2, "output_tokens": 1}


class _Retryable:
    """可重试策略桩（不真睡眠）。"""

    max_attempts = 3

    def should_retry(self, exc, attempt):
        return True

    def sleep(self, attempt):
        return None


# ------------------------------------------------------------------ 指标上报故障隔离


def test_metrics_failure_does_not_change_graph_wall_verdict(monkeypatch):
    """指标上报失败不影响图前的壁时间熔断结论。"""

    def _boom(*a, **kw):
        raise RuntimeError("metrics down")

    monkeypatch.setattr("hero_quant.metrics.inc_wall_time_exceeded", _boom)
    monkeypatch.setattr("hero_quant.metrics.observe_wall_time", _boom)
    res = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_GraphResult("unused"),
        wall_time_budget=1e-9,
        trace=False,
    ).run("q")
    assert res.reason == "wall_time_budget_exceeded"


def test_metrics_failure_does_not_change_wall_override(monkeypatch):
    """指标上报失败不影响"收尾超限覆盖 reason"的结论。"""

    def _boom(*a, **kw):
        raise RuntimeError("metrics down")

    monkeypatch.setattr("hero_quant.metrics.inc_wall_time_exceeded", _boom)

    class _SlowGraph:
        def invoke(self, state, config=None):
            threading.Event().wait(0.12)
            return {"messages": [{"content": "graph answer"}]}

    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_SlowGraph(), wall_time_budget=0.05, trace=False
    ).run("q")
    assert res.reason == "wall_time_budget_exceeded"
    assert res.text == "graph answer"


def test_metrics_failure_does_not_change_timeout_reason(monkeypatch):
    """超时指标上报失败时仍按 llm_timeout 收敛。"""

    def _boom(*a, **kw):
        raise RuntimeError("metrics down")

    monkeypatch.setattr("hero_quant.metrics.inc_llm_timeout", _boom)
    res = AgentLoop(
        llm=_ScriptedLLM([TimeoutError("slow")]), max_iterations=1, retry_policy=_NoRetry(), trace=False
    ).run("q")
    assert res.reason == "llm_timeout"


# ------------------------------------------------------------------ 其它兼容/降级分支


def test_trace_true_with_failed_writer_degrades_to_no_trace(tmp_path, monkeypatch):
    """trace=True 且默认回灌构造失败：降级为无 trace，主循环照常完成。"""
    monkeypatch.setenv("HERO_TRACE_DIR", str(tmp_path))

    def _boom(*a, **kw):
        raise OSError("trace dir unwritable")

    monkeypatch.setattr("hero_quant.agent.trace.TraceWriter", _boom)
    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), max_iterations=2, trace=True)
    assert loop._ensure_trace_writer() is None
    assert loop.run("q").text == "hi"


def test_writeback_falls_back_to_single_arg_then_two_arg():
    """记忆回写兼容 (key, content) / (content) 两种签名：单参失败后回退双参。"""
    calls = []

    class _Store:
        def write(self, key, content=None):
            calls.append((key, content))
            if content is None:
                raise RuntimeError("single-arg form unsupported")
            if len(calls) == 1:
                raise TypeError("two-arg form not ready")

    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "final answer"}]]), memory_store=_Store(), trace=False
    ).run("q")
    assert res.reason == "completed"
    assert calls[0][1] == "final answer"
    assert calls[-1] == calls[0]


def test_recall_fallback_chain_failure_returns_empty_hits(caplog):
    """recall 的三种调用形态都失败时按空命中处理（告警不阻断）。"""

    class _Store:
        def recall(self, query):
            raise TypeError("needs top_k")

    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        res = AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]),
            max_iterations=2,
            memory_store=_Store(),
            trace=False,
        ).run("q")
    assert res.reason == "completed"
    assert "memory recall failed" in caplog.text


def test_replay_usage_sidecar_write_failure_does_not_break(tmp_path):
    """回放侧车写盘失败（目标目录不可创建）不影响回放结果。"""
    replay = tmp_path / "replay.json"
    replay.write_text('{"llm_usage": {"input_tokens": 1, "output_tokens": 1}, "text": "replayed"}', encoding="utf-8")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(tmp_path),
        trace=_Recorder(str(blocked / "trace.jsonl")),
    )
    res = loop.run("q")
    assert res.text == "replayed"
    assert res.reason == "completed"


def test_grounding_snapshot_evidence_failure_degrades(tmp_path, monkeypatch):
    """读取证据快照抛异常时不做 authorized 注入，但仍执行工具（不因此中断）。"""

    class _RaisingLedger:
        @property
        def _evidence(self):
            raise RuntimeError("evidence store broken")

        def assert_price(self, symbol, price, authorized=None):
            return None

    received = []

    def _fn(authorized="unset", **_kwargs):
        received.append(authorized)
        return "ok"

    _register(monkeypatch, "wt_snapshot_tool", _fn)
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "tool_call", "name": "wt_snapshot_tool", "arguments": {}}]]),
        max_iterations=2,
        grounding=_RaisingLedger(),
        trace=False,
    ).run("q")
    assert received == ["unset"]  # 未注入 authorized 参数
    assert res.reason == "completed"


@pytest.mark.parametrize("claim", [{"type": "price", "value": ["100.0"]}, {"type": "price", "value": "about 100.0"}])
def test_graph_grounding_claims_value_forms_are_supported(monkeypatch, claim):
    """图路径同样支持 claims 的 list 形态与非数值（回退 raw 抽数字）。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", lambda _t: [dict(claim)])
    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_GraphResult("AAPL close 100.0"), grounding=ledger, trace=False
    ).run("q")
    assert res.grounding_verified is True


def test_grounding_claim_without_digits_is_ignored():
    """claim 的 value/raw 里没有数字时不产生价格候选（不会凭空构造断言）。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "AAPL looks fine"}]]),
        max_iterations=2,
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.grounding_verified is True
    assert res.reason == "completed"


def test_grounding_fallback_accepts_string_price_when_ledger_lenient():
    """回退首个 symbol 路径下，账本接受原始字符串价格时一次通过。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    res = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "close 100.0"}]]),
        max_iterations=2,
        grounding=ledger,
        trace=False,
    ).run("q")
    assert res.grounding_verified is True


def test_graph_grounding_lenient_ledger_accepts_string_price():
    """图路径回退首个 symbol 时，账本接受字符串价格即通过。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_GraphResult("close 100.0"), grounding=ledger, trace=False
    ).run("q")
    assert res.grounding_verified is True


def test_graph_grounding_evidence_failure_in_regex_fallback_fails_verified(monkeypatch):
    """图路径回退正则后读取证据仍失败：判为未验证（fail-visible），不静默放行。"""

    def _boom(_text):
        raise RuntimeError("claim extractor broken")

    class _RaisingLedger:
        @property
        def _evidence(self):
            raise RuntimeError("evidence store broken")

        def assert_price(self, symbol, price, authorized=None):
            return None

    monkeypatch.setattr("hero_quant.agent.grounding.extract_claims", _boom)
    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_GraphResult("AAPL close 100.0"), grounding=_RaisingLedger(), trace=False
    ).run("q")
    assert res.grounding_verified is False


# ------------------------------------------------------------------ 重试策略不可用


def test_loop_survives_unavailable_retry_policy(monkeypatch):
    """重试策略类不可用时退化为"无策略"（未超次数即重试 + 线性退避），主循环仍能完成。"""
    from hero_quant.agent import loop as loop_mod

    def _boom(*a, **kw):
        raise RuntimeError("policies unavailable")

    monkeypatch.setattr(loop_mod, "_RetryPolicy", None)
    monkeypatch.setattr("hero_quant.agent.policies.RetryPolicy", _boom)
    llm = _ScriptedLLM([RuntimeError("transient"), RuntimeError("transient"), [{"type": "text", "text": "recovered"}]])
    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert res.text == "recovered"
    assert res.reason == "completed"
    assert llm.calls == 3  # 默认 max_attempts=3 内重试成功


def test_zero_attempt_budget_skips_llm_call_and_converges():
    """重试预算为 0（max_attempts<=0）时不调用 LLM，按空流继续并由轮次上限收敛。"""

    class _NoAttempts:
        max_attempts = 0

        def should_retry(self, exc, attempt):
            return False

        def sleep(self, attempt):
            return None

    llm = _ScriptedLLM([[{"type": "text", "text": "never"}]])

    class _DuckWriter:
        """path 正常但 dir_path 属性损坏的 writer（用于验证 trace_path 兜底）。"""

        path = None

        def append(self, obj):
            return None

        @property
        def dir_path(self):
            raise RuntimeError("dir_path broken")

    res = AgentLoop(llm=llm, max_iterations=1, retry_policy=_NoAttempts(), trace=_DuckWriter()).run("q")
    assert llm.calls == 0
    assert res.reason == "max_iterations"
    assert res.iterations == 1
    assert res.trace_path is None  # 损坏的 writer 不影响 trace_path 兜底


# ------------------------------------------------------------------ skills 摘要获取失败


def test_skills_descriptions_failure_is_warned(caplog):
    """get_descriptions 抛异常时只记 debug 日志，不阻断主循环。"""

    class _BrokenSkills:
        def get_descriptions(self):
            raise RuntimeError("skills index broken")

    ctx = _Ctx()
    with caplog.at_level(logging.DEBUG, logger="hero_quant.agent.loop"):
        res = AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
            max_iterations=1,
            context_manager=ctx,
            memory_store=_StoreEmpty(),
            skills_loader=_BrokenSkills(),
            trace=False,
        ).run("q")
    assert res.reason == "completed"
    assert "skills_digest block failed" in caplog.text


def test_skills_prompt_build_failure_is_warned(caplog):
    """build_system_prompt 抛非 TypeError 异常时只记 debug 日志，摘要注入不中断。"""

    class _CtxBrokenPrompt(_Ctx):
        def build_system_prompt(self, skills_digest=None):
            raise RuntimeError("prompt builder broken")

    ctx = _CtxBrokenPrompt()
    with caplog.at_level(logging.DEBUG, logger="hero_quant.agent.loop"):
        res = AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
            max_iterations=1,
            context_manager=ctx,
            memory_store=_StoreEmpty(),
            skills_loader=_Skills(),
            trace=False,
        ).run("q")
    assert res.reason == "completed"
    assert "skills_digest prompt build failed" in caplog.text


def test_skills_prompt_no_arg_compensation_failure_is_warned(caplog):
    """无参签名补偿调用也失败时记 debug 日志（不抛）。"""

    class _CtxBothBroken(_Ctx):
        """带参调用抛 TypeError（走无参补偿），无参调用也失败。"""

        def __init__(self):
            super().__init__()
            self.calls = 0

        def build_system_prompt(self, skills_digest=None):
            self.calls += 1
            if self.calls == 1:
                raise TypeError("kwargs form unsupported")
            raise RuntimeError("no-arg form broken")

    ctx = _CtxBothBroken()
    with caplog.at_level(logging.DEBUG, logger="hero_quant.agent.loop"):
        res = AgentLoop(
            llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]),
            max_iterations=1,
            context_manager=ctx,
            memory_store=_StoreEmpty(),
            skills_loader=_Skills(),
            trace=False,
        ).run("q")
    assert res.reason == "completed"
    assert "skills_digest compensate failed" in caplog.text


def test_graph_grounding_claim_without_digits_is_ignored():
    """图路径 claim 的 value/raw 无数字时不产生价格候选（视为无幻觉）。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    res = AgentLoop(
        llm=_ScriptedLLM([[]]), use_graph=True, graph=_GraphResult("AAPL looks fine"), grounding=ledger, trace=False
    ).run("q")
    assert res.grounding_verified is True
