"""Round7 X4 收尾补测：agent/loop.py 仍未被 round7/w4 用例覆盖的兜底分支。

只补测试、不改 src。每个用例自包含：LLM / trace writer / context / ledger / 工具
均为本地假实现；异常路径通过 monkeypatch 精确注入，作用域由 pytest 自动回收，
不写全局测试夹具、不依赖执行顺序。

覆盖要点（按 loop.py 行号）：
- estimate_tokens 的 json/bytes/len 多重兜底（47, 67-111）
- 构造期参数校验与历史 kwargs 别名（166-199）
- replay 白名单解析/临时目录探测/路径穿越校验的异常分支（264-327）
- 记忆召回非 dict 命中、回写属性访问失败（513, 575-576）
- 壁时间预算不可用、图路径 reason/指标异常（627-657, 1804-1805）
- replay 侧车落盘 / 路径解析异常（756, 767-768, 781-782, 795-796）
- 注入与重试退避的二级兜底（884-885, 921-927）
- chunk 用量/打印异常、重试指标/流错误 trace 异常（1033-1041, 1122-1140）
- 工具调用解析/回填/注册表/双层脱敏异常链（1166-1258）
- 授权快照注入的签名探测与异常吞没（1305-1313）
- 工具结果脱敏三级兜底、并发超时取消/结果异常、shutdown 兼容（1336-1427）
- 接地校验 raw 回退、正则兜底失败、非字符串 symbol fail-closed（1480-1548）
- 上下文 add 探测异常、用量侧车 dir_path、收尾回写异常（1560-1561, 1731, 1751-1752）
- 图路径文本转换/get/path/trace/claims/指标兜底（1870-1910, 1982-1983）

断言均为具体 reason / 文本片段 / trace 事件字段 / 调用参数 / 异常类型。
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import tempfile
import threading
from pathlib import Path

import pytest

from hero_quant.agent import loop as loop_mod
from hero_quant.agent.context import CompactResult
from hero_quant.agent.grounding import GroundingLedger
from hero_quant.agent.loop import AgentLoop, LoopResult, estimate_tokens
from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec

_PROV = {"source": "test", "unit": "shares"}


# ------------------------------------------------------------------ 夹具


def _raiser(exc_type, message):
    """构造指定异常类型的一次性触发函数（用于注入口依赖故障）。"""

    def _fn(*_args, **_kwargs):
        raise exc_type(message)

    return _fn


def _iter_chunks(step):
    """把脚本项变成流：项内的 Exception 代表"流中途"失败。"""
    for chunk in step:
        if isinstance(chunk, BaseException):
            raise chunk
        yield chunk


class _ScriptedLLM:
    """按脚本产出 chunk 的假 LLM（与既有 round7 用例同款语义）。"""

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
    """trace 鸭子类型写入器：path + append，默认落临时目录避免污染仓库根。"""

    def __init__(self, path=None):
        if path is None:
            path = str(Path(tempfile.mkdtemp(prefix="round7-gap-")) / "trace.jsonl")
        self.path = path
        self.events = []

    def append(self, obj):
        self.events.append(obj)

    def first(self, type_name):
        for event in self.events:
            if event.get("type") == type_name:
                return event
        return None


class _BrokenWriter:
    """append 恒失败的写入器：trace 是旁路，不得反噬业务。"""

    def __init__(self, path="broken.jsonl"):
        self.path = path
        self.attempts = 0

    def append(self, obj):
        self.attempts += 1
        raise OSError("disk full")


class _LateBrokenPathWriter:
    """构造期 path 可读、运行期 path 抛错的 writer（armed 后生效）。"""

    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.events = []
        self.armed = False

    def append(self, obj):
        self.events.append(obj)

    @property
    def path(self):
        if self.armed:
            raise RuntimeError("path broken")
        return None


class _DirOnlyWriter:
    """只暴露 dir_path（无 path）的 writer，验证侧车目录解析分支。"""

    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.events = []

    def append(self, obj):
        self.events.append(obj)


class _GraphBrokenWriter:
    """图路径用：append 与 path 都坏（path 在 armed 后抛错）。"""

    def __init__(self):
        self.armed = False

    def append(self, obj):
        raise OSError("graph trace broken")

    @property
    def path(self):
        if self.armed:
            raise RuntimeError("graph path broken")
        return None


class _Retry:
    """可编程重试策略桩（不真睡眠）。"""

    def __init__(self, *, max_attempts=2, retry=True, sleep_raises=False):
        self.max_attempts = max_attempts
        self._retry = retry
        self._sleep_raises = sleep_raises
        self.slept = []

    def should_retry(self, exc, attempt):
        return self._retry

    def sleep(self, attempt):
        self.slept.append(attempt)
        if self._sleep_raises:
            raise RuntimeError("sleep broken")


class _Ctx:
    """最小上下文管理器：记录 add、可控 compact 结果。"""

    def __init__(self, messages=None, compact_result=None):
        self._messages = list(messages or [])
        self._compact_result = compact_result
        self.added = []
        self.compact_calls = 0

    def add(self, role, content):
        self.added.append((role, content))

    def compact(self):
        self.compact_calls += 1
        return self._compact_result


class _AddProbeBoomCtx(_Ctx):
    """hasattr(ctx, "add") 探测本身抛异常的上下文（1560-1561）。"""

    @property
    def add(self):
        raise RuntimeError("add probe broken")


class _Graph:
    """图鸭类型：可控返回值/异常。"""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def invoke(self, state, config=None):
        self.calls.append(state)
        if self.error is not None:
            raise self.error
        return self.result


def _register(monkeypatch, name, func, *, safe=False, timeout_ms=None):
    spec = ToolSpec(
        name=name,
        description=f"round7 gap tool {name}",
        func=func,
        is_concurrency_safe=(lambda args: safe) if isinstance(safe, bool) else safe,
        timeoutMs=timeout_ms,
    )
    monkeypatch.setitem(TOOL_REGISTRY, name, spec)
    return spec



def _plain_relative_to(self, other, *_args, **_kwargs):
    """模拟 Python<3.9 的 relative_to 实现（不依赖 is_relative_to）。"""
    self_s = str(self)
    other_s = str(other).rstrip("/") or "/"
    if self_s == other_s or self_s.startswith(other_s + "/"):
        return Path(self_s[len(other_s):].lstrip("/") or ".")
    raise ValueError(f"{self_s} is not in the subpath of {other_s}")


def _write_replay(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------ _warn / estimate_tokens


def test_warn_returns_early_when_structlog_is_missing(caplog, monkeypatch):
    """_slog 不可用时 _warn 只走 stdlib warning，不得因缺失结构化通道报错。"""
    monkeypatch.setattr(loop_mod, "_slog", None)
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        assert loop_mod._warn("gap_op", "stdlib only %s", "ok") is None
    assert "stdlib only ok" in caplog.text


def test_estimate_tokens_list_json_dump_returning_non_str_is_coerced(monkeypatch):
    """list 分支 json.dumps 返回非 str 时先 str() 再按 4 字符/token 计。"""
    monkeypatch.setattr(json, "dumps", lambda *a, **k: 12345)
    assert estimate_tokens([{"a": 1}]) == 1


def test_estimate_tokens_list_dump_failure_falls_back_to_str(monkeypatch):
    """list 的 json 序列化失败时回退 len(str(text))，不得抛异常。"""
    monkeypatch.setattr(json, "dumps", _raiser(RuntimeError, "dump broken"))
    assert estimate_tokens([1, 2, 3]) == len(str([1, 2, 3])) // 4


def test_estimate_tokens_list_dump_and_str_both_fail_returns_zero(monkeypatch):
    """list 的 json 与 str 双双失败时收敛为 0（fail-visible 且不阻断）。"""

    class _UnprintableList(list):
        def __str__(self):
            raise RuntimeError("no str")

    monkeypatch.setattr(json, "dumps", _raiser(RuntimeError, "dump broken"))
    assert estimate_tokens(_UnprintableList([1])) == 0


def test_estimate_tokens_bytes_decode_failure_uses_repr_fallback():
    """bytes.decode 抛错时回退 str(bytes) 的长度，避免崩溃。"""

    class _UndecodableBytes(bytes):
        def decode(self, *_args, **_kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    raw = _UndecodableBytes(b"abcd")
    assert estimate_tokens(raw) == len(str(raw)) // 4 == 1


def test_estimate_tokens_dict_json_dump_returning_non_str_is_coerced(monkeypatch):
    """dict 分支 json.dumps 返回非 str 时先 str() 再计数。"""
    monkeypatch.setattr(json, "dumps", lambda *a, **k: 99999)
    assert estimate_tokens({"a": 1}) == 1


def test_estimate_tokens_dict_dump_failure_falls_back_to_str(monkeypatch):
    """dict 的 json 序列化失败时回退 len(str(dict))。"""
    monkeypatch.setattr(json, "dumps", _raiser(RuntimeError, "dump broken"))
    assert estimate_tokens({"a": 1}) == len(str({"a": 1})) // 4


def test_estimate_tokens_dict_dump_and_str_both_fail_returns_zero(monkeypatch):
    """dict 的 json 与 str 双双失败时收敛为 0。"""

    class _UnprintableDict(dict):
        def __str__(self):
            raise RuntimeError("no str")

    monkeypatch.setattr(json, "dumps", _raiser(RuntimeError, "dump broken"))
    assert estimate_tokens(_UnprintableDict(a=1)) == 0


def test_estimate_tokens_hostile_object_returns_zero():
    """既无 len 也无法 str 的对象最终返回 0（日志兜底后失败）。"""

    class _Hostile:
        def __len__(self):
            raise TypeError("no len")

        def __str__(self):
            raise ValueError("no str")

    assert estimate_tokens(_Hostile()) == 0


# ------------------------------------------------------------------ 构造期校验 / 别名


def test_constructor_clamps_invalid_max_iterations_and_token_limit(caplog):
    """非法 max_iterations/token_limit 全部回退到安全默认值并告警。"""
    llm = _ScriptedLLM([[]])
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        assert AgentLoop(llm=llm, max_iterations=0, trace=False).max_iterations == 5
        assert AgentLoop(llm=llm, max_iterations="oops", trace=False).max_iterations == 5
        assert AgentLoop(llm=llm, token_limit=None, trace=False).token_limit is None
        assert AgentLoop(llm=llm, token_limit=-5, trace=False).token_limit is None
        assert AgentLoop(llm=llm, token_limit="oops", trace=False).token_limit == 60000

    assert "max_iterations 0 <=0 clamped to 5" in caplog.text
    assert "invalid max_iterations 'oops'" in caplog.text
    assert "token_limit -5 <=0 treated as unlimited" in caplog.text
    assert "invalid token_limit 'oops'" in caplog.text


def test_constructor_legacy_context_alias_is_wired():
    """历史别名 context= 仍被接到 context_manager/context 两个出口。"""
    ctx = _Ctx()
    loop = AgentLoop(llm=_ScriptedLLM([[]]), context=ctx, trace=False)
    assert loop.context_manager is ctx
    assert loop.context is ctx


# ------------------------------------------------------------------ replay 白名单解析


def test_allow_root_home_directory_is_rejected_as_too_broad(tmp_path, caplog):
    """allow_root 指向家目录时按"过宽"拒绝并回退默认目录，最终 fail-closed。"""
    replay = _write_replay(tmp_path / "r.json", "x")
    with caplog.at_level(logging.WARNING, logger="hero_quant.agent.loop"):
        with pytest.raises(ValueError, match="outside allowed directory"):
            AgentLoop(
                llm=_ScriptedLLM([[]]),
                replay_path=str(replay),
                allow_root=str(Path.home()),
                trace=False,
            )
    assert "too broad, falling back to default replays dir" in caplog.text


def test_allow_root_replays_named_dir_survives_tempdir_probe_failure(tmp_path, monkeypatch):
    """临时目录探测失败时仍认"目录名为 replays"的显式 allow_root。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    trusted = tmp_path / "nested" / "replays"
    replay = _write_replay(trusted / "r.json", "trusted replay")
    monkeypatch.setattr(tempfile, "gettempdir", _raiser(RuntimeError, "no tmp dir"))

    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(trusted),
        trace=False,
    )
    assert loop.run("q").text == "trusted replay"


def test_default_replays_exists_probe_failure_falls_back_to_cwd(tmp_path, monkeypatch):
    """默认 replays 目录的 exists() 探测失败时回退 cwd/replays，回放仍可读。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    replay = _write_replay(proj / "replays" / "r.json", "cwd replay")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(Path, "exists", _raiser(RuntimeError, "stat broken"))

    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), trace=False)
    monkeypatch.undo()
    assert loop.run("q").text == "cwd replay"


def test_replay_inside_allow_dir_without_is_relative_to(tmp_path, monkeypatch):
    """Path.is_relative_to 缺失（旧解释器）时用 relative_to 兜底判定为白名单内。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    replay = _write_replay(proj / "replays" / "r.json", "inside replay")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(Path, "is_relative_to", _raiser(AttributeError, "no is_relative_to"))
    monkeypatch.setattr(Path, "relative_to", _plain_relative_to)

    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay), trace=False)
    assert loop.run("q").text == "inside replay"


def test_replay_outside_allow_dir_without_is_relative_to_is_rejected(tmp_path, monkeypatch):
    """relative_to 兜底也失败时判定为白名单外，路径穿越被拒。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "replays").mkdir()
    outside = _write_replay(tmp_path / "r.json", "outside")
    monkeypatch.chdir(proj)
    monkeypatch.delenv("HERO_ALLOW_TMP_REPLAY", raising=False)
    monkeypatch.setattr(Path, "is_relative_to", _raiser(AttributeError, "no is_relative_to"))
    monkeypatch.setattr(Path, "relative_to", _raiser(ValueError, "outside trusted root"))

    with pytest.raises(ValueError, match="outside allowed directory"):
        AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(outside), trace=False)


def test_tmp_replay_allowed_without_is_relative_to_when_opted_in(tmp_path, monkeypatch):
    """HERO_ALLOW_TMP_REPLAY=1 且 is_relative_to 缺失时，用 relative_to(tmp) 放行临时回放件。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "replays").mkdir()
    outside = _write_replay(tmp_path / "r.json", "tmp replay")
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert outside.resolve().is_relative_to(tmp_root)  # 前置：回放件确实在系统临时目录内
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HERO_ALLOW_TMP_REPLAY", "1")
    monkeypatch.setattr(Path, "is_relative_to", _raiser(AttributeError, "no is_relative_to"))
    monkeypatch.setattr(Path, "relative_to", _plain_relative_to)

    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(outside), trace=False)
    assert loop.run("q").text == "tmp replay"


def test_tmp_replay_probe_failure_stays_fail_closed(tmp_path, monkeypatch):
    """临时目录探测自身失败时保持外部判定（fail-closed），不得静默放行。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "replays").mkdir()
    outside = _write_replay(tmp_path / "r.json", "leak")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HERO_ALLOW_TMP_REPLAY", "1")
    monkeypatch.setattr(Path, "is_relative_to", _raiser(AttributeError, "no is_relative_to"))
    monkeypatch.setattr(Path, "relative_to", _raiser(ValueError, "outside trusted root"))
    monkeypatch.setattr(tempfile, "gettempdir", _raiser(RuntimeError, "tmp probe broken"))

    with pytest.raises(ValueError, match="outside allowed directory"):
        AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(outside), trace=False)


# ------------------------------------------------------------------ 记忆注入 / 回写


def test_inject_string_hit_is_wrapped_as_content():
    """召回命中不是 dict 时按 str(hit) 注入，仍带 [memory recall] 前缀。"""

    class _Store:
        def recall(self, query):
            return ["plain note"]

    ctx = _Ctx()
    loop = AgentLoop(llm=_ScriptedLLM([[]]), memory_store=_Store(), context_manager=ctx, trace=False)
    assert loop.inject("q") == ["plain note"]
    assert ctx.added == [("system", "[memory recall] plain note")]


def test_writeback_swallows_store_attribute_failure():
    """写回时存储属性访问失败被最外层 except 吞掉，不向调用方冒泡。"""

    class _Store:
        def __init__(self):
            self.lookups = []

        def __getattr__(self, name):
            self.lookups.append(name)
            raise RuntimeError("store attribute broken")

    store = _Store()
    loop = AgentLoop(llm=_ScriptedLLM([[]]), memory_store=store, trace=False)
    assert loop._writeback("content") is None
    assert store.lookups == ["write"]


# ------------------------------------------------------------------ 壁时间预算


def test_unusable_wall_budget_disables_check_and_final_override():
    """壁时间值无法 float 时 _wall_exceeded 返回 False，收尾覆盖分支也静默跳过。"""
    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "hi"}]]), max_iterations=1, trace=False)
    loop.wall_time_budget = object()
    res = loop.run("q")
    assert res.reason == "completed"
    assert res.text == "hi"
    assert res.terminated is True


def test_graph_wall_budget_reason_survives_metrics_failure(monkeypatch):
    """图路径 reason 为 wall_time_budget_exceeded 时补记指标；指标失败不改判定。"""
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_Graph({"messages": [{"content": "unused"}]}),
        trace=False,
    )
    monkeypatch.setattr(
        loop,
        "_run_graph",
        lambda goal: LoopResult(
            terminated=True,
            iterations=1,
            text="graph wall",
            reason="wall_time_budget_exceeded",
            token_count=1,
        ),
    )
    monkeypatch.setattr("hero_quant.metrics.inc_wall_time_exceeded", _raiser(RuntimeError, "metric broken"))

    res = loop.run("q")
    assert res.reason == "wall_time_budget_exceeded"
    assert res.text == "graph wall"


# ------------------------------------------------------------------ replay 侧车 / 故障降级


def test_replay_with_dir_only_writer_reports_no_trace_path(tmp_path):
    """dir_path-only writer：侧车写入 dir_path，运行期 path 抛错时 trace_path 为 None。"""
    replay = _write_replay(tmp_path / "replays" / "r.json", "replayed")
    trace_dir = tmp_path / "trace_dir"
    writer = _LateBrokenPathWriter(str(trace_dir))
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(replay.parent),
        trace=writer,
    )
    writer.armed = True

    res = loop.run("q")
    assert res.text == "replayed"
    assert res.trace_path is None
    sidecar = json.loads((trace_dir / "llm_usage.json").read_text(encoding="utf-8"))
    assert sidecar["text"] == "replayed"


def test_replay_resolve_failure_still_writes_sidecar(tmp_path, monkeypatch):
    """目标侧车与回放件 resolve() 失败时按"不同路径"处理，仍落盘 llm_usage.json。"""
    replay = _write_replay(tmp_path / "replays" / "r.json", "replayed")
    writer = _Recorder(str(tmp_path / "t.jsonl"))
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(replay.parent),
        trace=writer,
    )
    monkeypatch.setattr(Path, "resolve", _raiser(RuntimeError, "resolve broken"))

    res = loop.run("q")
    assert res.text == "replayed"
    assert (tmp_path / "llm_usage.json").exists()


def test_replay_probe_failure_degrades_to_live_llm(tmp_path, monkeypatch):
    """回放探测（is_dir）抛异常时降级走真实 LLM，不得把回放故障当致命错误。"""
    replay = _write_replay(tmp_path / "replays" / "r.json", "never used")
    loop = AgentLoop(
        llm=_ScriptedLLM([[{"type": "text", "text": "live answer"}]]),
        replay_path=str(replay),
        allow_root=str(replay.parent),
        trace=False,
    )
    monkeypatch.setattr(Path, "is_dir", _raiser(RuntimeError, "probe broken"))

    res = loop.run("q")
    assert res.reason == "completed"
    assert res.text == "live answer"


# ------------------------------------------------------------------ 注入 / 重试退避


def test_inject_exception_does_not_abort_iteration(monkeypatch):
    """每轮 recall 注入抛异常时被吞掉，主循环继续完成。"""
    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]), max_iterations=2, trace=False)
    calls = []

    def _boom(goal):
        calls.append(goal)
        raise RuntimeError("inject broken")

    monkeypatch.setattr(loop, "inject", _boom)
    res = loop.run("q")
    assert calls == ["q"]
    assert res.reason == "completed"
    assert res.text == "answer"


def test_call_llm_retry_sleep_failure_uses_fixed_backoff(monkeypatch):
    """策略 sleep 与兜底 time.sleep 都失败时按"不睡眠"继续重试，第二次成功。"""
    llm = _ScriptedLLM([RuntimeError("transient"), [{"type": "text", "text": "recovered"}]])
    policy = _Retry(max_attempts=2, sleep_raises=True)
    monkeypatch.setattr(loop_mod.time, "sleep", _raiser(RuntimeError, "sleep broken"))

    res = AgentLoop(llm=llm, max_iterations=2, retry_policy=policy, trace=False).run("q")
    assert res.text == "recovered"
    assert res.reason == "completed"
    assert llm.calls == 2


def test_missing_retry_policy_sleep_failure_is_ignored(monkeypatch):
    """重试策略不可用（None）时按 attempt 预算重试；time.sleep 失败也不得中断。"""
    monkeypatch.setattr(loop_mod, "_RetryPolicy", None)
    monkeypatch.setattr("hero_quant.agent.policies.RetryPolicy", _raiser(RuntimeError, "policy unavailable"))
    monkeypatch.setattr(loop_mod.time, "sleep", _raiser(RuntimeError, "sleep broken"))
    llm = _ScriptedLLM([RuntimeError("transient"), [{"type": "text", "text": "recovered"}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert res.text == "recovered"
    assert res.reason == "completed"
    assert llm.calls == 2


# ------------------------------------------------------------------ chunk 归一化异常


def test_usage_metadata_access_failure_is_ignored():
    """读取 usage_metadata 抛异常时整段用量统计跳过，文本仍正常累积。"""

    class _UsageProbeBoomDict(dict):
        def get(self, key, default=None):
            if key == "usage_metadata":
                raise RuntimeError("usage probe broken")
            return super().get(key, default)

    chunk = _UsageProbeBoomDict({"type": "text", "text": "hi"})
    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=1, trace=False).run("q")
    assert res.text == "hi"
    assert res.reason == "completed"


def test_chunk_stringify_failure_is_ignored():
    """非 dict/str chunk 的 str() 失败时跳过该 chunk，后续轮次仍可完成。"""

    class _UnprintableChunk:
        def __str__(self):
            raise RuntimeError("chunk unprintable")

    llm = _ScriptedLLM([[_UnprintableChunk()], [{"type": "text", "text": "done"}]])
    res = AgentLoop(llm=llm, max_iterations=3, trace=False).run("q")
    assert res.reason == "completed"
    assert res.text == "done"


# ------------------------------------------------------------------ 流重试指标 / trace


def test_stream_retry_metric_failure_does_not_block_retry(monkeypatch):
    """重试计数指标上报失败不阻断重试，下一轮流成功即完成。"""
    monkeypatch.setattr("hero_quant.metrics.inc_llm_retry", _raiser(RuntimeError, "metric broken"))
    llm = _ScriptedLLM(
        [
            [{"type": "text", "text": "head"}, RuntimeError("stream broke")],
            [{"type": "text", "text": "final"}],
        ]
    )
    res = AgentLoop(llm=llm, max_iterations=3, retry_policy=_Retry(max_attempts=3), trace=False).run("q")
    assert res.reason == "completed"
    assert res.text == "final"
    assert llm.calls == 2


def test_stream_terminal_error_with_broken_trace_keeps_reason(tmp_path):
    """不可重试流异常时 trace 写入失败，错误仍收敛为 llm_error 并保留错误文本。"""
    llm = _ScriptedLLM([[{"type": "text", "text": "head "}, RuntimeError("stream boom")]])
    writer = _BrokenWriter(str(tmp_path / "broken.jsonl"))
    res = AgentLoop(
        llm=llm,
        max_iterations=2,
        retry_policy=_Retry(max_attempts=3, retry=False),
        trace=writer,
    ).run("q")
    assert res.reason == "llm_error"
    assert "[ERROR: stream boom]" in res.text
    assert writer.attempts > 0


def test_stream_timeout_metric_failure_keeps_timeout_reason(monkeypatch):
    """流超时且指标上报失败时仍以 llm_timeout 收敛（可观测失败不改判定）。"""
    monkeypatch.setattr("hero_quant.metrics.inc_llm_timeout", _raiser(RuntimeError, "metric broken"))
    llm = _ScriptedLLM([[{"type": "text", "text": "head "}, TimeoutError("stream slow")]])
    res = AgentLoop(
        llm=llm,
        max_iterations=2,
        retry_policy=_Retry(max_attempts=3, retry=False),
        trace=False,
    ).run("q")
    assert res.reason == "llm_timeout"
    assert "[ERROR: stream slow]" in res.text


# ------------------------------------------------------------------ 工具调用解析 / 回填 / 注册表 / 脱敏


def test_symbol_fallback_regex_failure_keeps_original_args(monkeypatch):
    """goal 抽取 symbol 的正则崩溃时回填列表为空，带 symbol 需求的工具参数保持原样。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "get_market_data", _fn)
    monkeypatch.setattr(loop_mod.re, "findall", _raiser(RuntimeError, "regex broken"))
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "get_market_data", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert seen == [{}]
    assert res.reason == "completed"


def test_autofill_keeps_explicit_symbol(monkeypatch):
    """调用方已给 symbol 时回填函数原样返回，不覆盖显式参数。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "get_market_data", _fn)
    llm = _ScriptedLLM(
        [[{"type": "tool_call", "name": "get_market_data", "arguments": {"symbol": "600519.SH"}}]]
    )

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("600519.SH")
    assert seen == [{"symbol": "600519.SH"}]
    assert res.reason == "completed"


def test_tool_call_with_empty_function_envelope_is_skipped(monkeypatch):
    """function 为空的工具调用取不到名字被丢弃，同一批次的合法调用照常执行。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w7_keep_tool", _fn)
    chunks = [
        {"type": "tool_call", "function": {}},
        {"type": "tool_call", "name": "w7_keep_tool", "arguments": {}},
    ]
    res = AgentLoop(llm=_ScriptedLLM([chunks]), max_iterations=2, trace=False).run("q")
    assert seen == [{}]
    assert "[tool w7_keep_tool result] ok" in res.text
    assert res.reason == "completed"


def test_bad_json_args_logging_failure_is_swallowed(monkeypatch):
    """坏 JSON 参数告警日志自身抛错时被吞掉，参数降级为 {} 后工具仍被调用。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "w7_json_tool", _fn)

    class _BoomLogging:
        def getLogger(self, *args, **kwargs):
            return self

        def warning(self, *args, **kwargs):
            raise RuntimeError("log broken")

        def debug(self, *args, **kwargs):
            raise RuntimeError("log broken")

    monkeypatch.setattr(loop_mod, "logging", _BoomLogging())
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_json_tool", "arguments": "{bad json"}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert seen == [{}]
    assert res.reason == "completed"


def test_symbol_autofill_failure_keeps_args(monkeypatch):
    """symbol 无法 str() 时回填异常被吞，原始参数（含对象）原样传给工具。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "get_market_data", _fn)

    class _StubbornSymbol:
        def __str__(self):
            raise RuntimeError("symbol unprintable")

    symbol = _StubbornSymbol()
    llm = _ScriptedLLM(
        [[{"type": "tool_call", "name": "get_market_data", "arguments": {"symbol": symbol}}]]
    )

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert seen == [{"symbol": symbol}]
    assert res.reason == "completed"


def test_registry_lookup_failure_becomes_tool_not_found(monkeypatch):
    """TOOL_REGISTRY.get 抛错时按"未知工具"处理，不冒泡到主循环。"""

    class _BoomRegistry:
        def get(self, name):
            raise RuntimeError("registry broken")

    monkeypatch.setattr("hero_quant.tools.registry.TOOL_REGISTRY", _BoomRegistry())
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_any_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert "tool_not_found: w7_any_tool" in res.text
    assert res.reason == "max_iterations"


def test_argument_redaction_falls_back_to_tools_redactor(monkeypatch):
    """security.redact_payload 失败时退到 tools.redaction._maybe_redact（fail-closed 哨兵）。"""
    _register(monkeypatch, "w7_redact_tool", lambda **kwargs: "ok")
    monkeypatch.setattr(
        "hero_quant.security.redaction.redact_payload",
        _raiser(RuntimeError, "primary redactor broken"),
    )
    trace = _Recorder()
    llm = _ScriptedLLM(
        [[{"type": "tool_call", "name": "w7_redact_tool", "arguments": {"api_key": "SECRET"}}]]
    )

    res = AgentLoop(llm=llm, max_iterations=2, trace=trace).run("q")
    call = trace.first("tool_call")
    assert call["arguments"] == "***"
    assert res.reason == "completed"


def test_argument_redaction_double_failure_keeps_raw_args(monkeypatch):
    """两层脱敏都失败时原样保留参数（fail-open 到调用方审计，不丢参数）。"""
    _register(monkeypatch, "w7_redact_tool", lambda **kwargs: "ok")
    monkeypatch.setattr(
        "hero_quant.security.redaction.redact_payload",
        _raiser(RuntimeError, "primary redactor broken"),
    )
    monkeypatch.setattr(
        "hero_quant.tools.redaction._maybe_redact",
        _raiser(RuntimeError, "secondary redactor broken"),
    )
    trace = _Recorder()
    llm = _ScriptedLLM(
        [[{"type": "tool_call", "name": "w7_redact_tool", "arguments": {"k": "v"}}]]
    )

    res = AgentLoop(llm=llm, max_iterations=2, trace=trace).run("q")
    assert trace.first("tool_call")["arguments"] == {"k": "v"}
    assert res.reason == "completed"


# ------------------------------------------------------------------ 授权快照注入


def test_authorized_signature_probe_failure_skips_injection(monkeypatch):
    """inspect.signature 不可用时跳过 authorized 注入，工具按原参数执行。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "wt_sig_lookup_tool", _fn)
    monkeypatch.setattr("inspect.signature", _raiser(TypeError, "signature unsupported"))
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "wt_sig_lookup_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=GroundingLedger(), trace=False).run("q")
    assert seen == [{}]
    assert res.reason == "completed"


def test_authorized_injection_unexpected_spec_error_is_swallowed(monkeypatch):
    """读取 spec.signature 抛非 AttributeError 时整段注入跳过，工具照常执行。"""
    seen = []

    class _SpecProbeBoom:
        timeoutMs = None

        @property
        def signature(self):
            raise RuntimeError("signature probe broken")

        def is_concurrency_safe(self, args):
            return False

        def func(self, **kwargs):
            seen.append(kwargs)
            return "ok"

    monkeypatch.setitem(TOOL_REGISTRY, "wt_spec_probe_tool", _SpecProbeBoom())
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "wt_spec_probe_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=GroundingLedger(), trace=False).run("q")
    assert seen == [{}]
    assert "[tool wt_spec_probe_tool result] ok" in res.text


# ------------------------------------------------------------------ 工具结果脱敏 / 并发执行


def test_tool_result_redaction_fallback_uses_security_payload(monkeypatch):
    """redact_tool_result 不可用时用 security.redact_payload 处理 dict 结果。"""
    _register(monkeypatch, "w7_result_tool", lambda **kwargs: {"api_key": "SECRET"})
    monkeypatch.setattr(
        "hero_quant.tools.redaction.redact_tool_result",
        _raiser(RuntimeError, "result redactor broken"),
    )
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_result_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert "[tool w7_result_tool result]" in res.text
    assert "SECRET" not in res.text
    assert "***" in res.text


def test_tool_result_redaction_double_failure_falls_back_to_str(monkeypatch):
    """两层结果脱敏都失败时直接用 str(result)，保证 buffer 仍有工具输出。"""
    _register(monkeypatch, "w7_result_tool", lambda **kwargs: {"price": 1})
    monkeypatch.setattr(
        "hero_quant.tools.redaction.redact_tool_result",
        _raiser(RuntimeError, "result redactor broken"),
    )
    monkeypatch.setattr(
        "hero_quant.security.redaction.redact_payload",
        _raiser(RuntimeError, "payload redactor broken"),
    )
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_result_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert "[tool w7_result_tool result] {'price': 1}" in res.text
    assert res.reason == "completed"


def test_concurrent_timeout_cancel_failure_is_ignored(monkeypatch):
    """并发超时后 fut.cancel() 抛错被吞掉，工具仍以 timeout tool_error 收敛。"""
    _register(
        monkeypatch,
        "w7_timeout_tool",
        lambda **kwargs: threading.Event().wait(0.1),
        safe=True,
        timeout_ms=10,
    )
    monkeypatch.setattr(
        concurrent.futures.Future,
        "cancel",
        _raiser(RuntimeError, "cancel broken"),
    )
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_timeout_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert "timeout after 10ms (abandoned, worker not cancellable)" in res.text
    assert res.reason == "max_iterations"


def test_concurrent_future_result_failure_becomes_tool_error(monkeypatch):
    """fut.result() 抛非超时异常时收敛为 tool_error: <exc>，下一轮文本仍可完成。"""
    _register(monkeypatch, "w7_future_tool", lambda **kwargs: "ok", safe=True)
    monkeypatch.setattr(
        concurrent.futures.Future,
        "result",
        _raiser(RuntimeError, "future probe broken"),
    )
    llm = _ScriptedLLM(
        [
            [{"type": "tool_call", "name": "w7_future_tool", "arguments": {}}],
            [{"type": "text", "text": "done"}],
        ]
    )

    res = AgentLoop(llm=llm, max_iterations=3, trace=False).run("q")
    assert "tool_error: future probe broken" in res.text
    assert res.text.endswith("done")
    assert res.reason == "completed"


def test_executor_shutdown_compat_fallback(monkeypatch):
    """shutdown(cancel_futures=) 不支持时降级为 shutdown(wait=False)，再失败也不阻断。"""
    _register(monkeypatch, "w7_shutdown_tool", lambda **kwargs: "ok", safe=True)
    calls = []

    def _shutdown(self, wait=True, cancel_futures=False):
        calls.append((wait, cancel_futures))
        if cancel_futures:
            raise TypeError("cancel_futures unsupported")
        raise RuntimeError("shutdown broken")

    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "shutdown", _shutdown)
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "w7_shutdown_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert calls == [(False, True), (False, False)]
    assert "[tool w7_shutdown_tool result] ok" in res.text
    assert res.reason == "completed"


# ------------------------------------------------------------------ 接地校验（主循环）


def test_grounding_claim_value_falls_back_to_raw_digits(monkeypatch):
    """claim 的 value 非 list/数值时从 raw 抽数字参与校验（100.0 命中证据）。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    monkeypatch.setattr(
        "hero_quant.agent.grounding.extract_claims",
        lambda _text: [{"type": "price", "value": "about", "raw": "100.0"}],
    )
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False).run("q")
    assert res.grounding_verified is True
    assert res.reason == "completed"


def test_grounding_regex_fallback_failure_yields_no_numbers(monkeypatch):
    """extract_claims 与正则回退双失败时 nums 为空，按"无价格断言"通过。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    monkeypatch.setattr(
        "hero_quant.agent.grounding.extract_claims",
        _raiser(RuntimeError, "extract broken"),
    )
    monkeypatch.setattr(loop_mod.re, "findall", _raiser(RuntimeError, "regex broken"))
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False).run("q")
    assert res.grounding_verified is True
    assert res.reason == "completed"


def test_grounding_non_string_evidence_key_fails_closed():
    """证据 key 非字符串导致 sym in buffer 抛错时 fail-closed（grounding_failed + trace）。"""

    class _IntKeyLedger:
        _evidence = {123: {"close": 100.0}}

        def assert_price(self, symbol, price, authorized=None):
            raise AssertionError("unreachable: membership check fails first")

    trace = _Recorder()
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 100"}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=_IntKeyLedger(), trace=trace).run("q")
    assert res.reason == "grounding_failed"
    assert res.grounding_verified is False
    assert "requires string as left operand" in trace.first("grounding_error")["error"]


# ------------------------------------------------------------------ 上下文压缩 / 收尾


def test_context_add_probe_failure_is_ignored_during_compaction():
    """hasattr(ctx, add) 探测抛错时压缩流程继续，仍执行 compact 并保持 completed。"""
    ctx = _AddProbeBoomCtx(
        messages=[],
        compact_result=CompactResult(truncated=False, banner="", text=""),
    )
    llm = _ScriptedLLM([[{"type": "text", "text": "x" * 600}]])

    res = AgentLoop(llm=llm, max_iterations=1, token_limit=200, context_manager=ctx, trace=False).run("q")
    assert ctx.compact_calls == 1
    assert res.reason == "completed"


def test_usage_sidecar_written_for_dir_only_writer(tmp_path):
    """dir_path-only writer：用量侧车写入 dir_path/llm_usage.json，内容含 usage 与文本。"""
    writer = _DirOnlyWriter(str(tmp_path))
    chunk = {"type": "text", "text": "used", "usage_metadata": {"input_tokens": 3, "output_tokens": 4}}

    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=1, trace=writer).run("q")
    payload = json.loads((tmp_path / "llm_usage.json").read_text(encoding="utf-8"))
    assert payload["llm_usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert payload["text"] == "used"
    assert res.trace_path == str(tmp_path)


def test_writeback_exception_at_finish_is_swallowed(monkeypatch):
    """收尾回写抛异常时被吞掉，不影响已完成结果。"""

    class _Store:
        pass

    loop = AgentLoop(llm=_ScriptedLLM([[{"type": "text", "text": "answer"}]]), memory_store=_Store(), trace=False)
    called = []

    def _boom(text):
        called.append(text)
        raise RuntimeError("writeback broken")

    monkeypatch.setattr(loop, "_writeback", _boom)
    res = loop.run("q")
    assert called == ["answer"]
    assert res.reason == "completed"
    assert res.text == "answer"


def test_metrics_extraction_failure_leaves_metrics_none(monkeypatch):
    """sharpe 提取正则抛错时 metrics 保持 None，不影响结果返回。"""
    monkeypatch.setattr(loop_mod.re, "search", _raiser(RuntimeError, "regex broken"))
    llm = _ScriptedLLM([[{"type": "text", "text": "sharpe 1.5"}]])

    res = AgentLoop(llm=llm, max_iterations=1, trace=False).run("q")
    assert res.metrics is None
    assert res.reason == "completed"


# ------------------------------------------------------------------ 图路径兜底


class _BadGetResult(dict):
    """get() 抛错的 dict 子类：迫使图文本转换走 str(result) 兜底。"""

    def get(self, key, default=None):
        raise RuntimeError("get broken")


def test_graph_result_get_failure_falls_back_to_str():
    """图结果 get() 抛错时回退 str(result) 作为文本，不中断委托。"""
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_Graph(_BadGetResult()),
        trace=False,
    )
    res = loop.run("q")
    assert res.text == "{}"
    assert res.reason == "completed"


def test_graph_trace_failures_do_not_block_result():
    """图路径的 graph_result 落盘与 path 读取都失败时结果照常返回，trace_path 为 None。"""
    writer = _GraphBrokenWriter()
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_Graph({"messages": [{"content": "graph ok"}]}),
        trace=writer,
    )
    writer.armed = True

    res = loop.run("q")
    assert res.text == "graph ok"
    assert res.trace_path is None
    assert res.reason == "completed"


def test_graph_claim_value_falls_back_to_raw_digits(monkeypatch):
    """图路径 claim 的 value 非数值时同样从 raw 抽数字并命中证据。"""
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    monkeypatch.setattr(
        "hero_quant.agent.grounding.extract_claims",
        lambda _text: [{"type": "price", "value": "about", "raw": "100.0"}],
    )
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_Graph({"messages": [{"content": "AAPL close 100.0"}]}),
        grounding=ledger,
        trace=False,
    )

    res = loop.run("q")
    assert res.text == "AAPL close 100.0"
    assert res.grounding_verified is True


def test_graph_metrics_extraction_failure_is_swallowed(monkeypatch):
    """图路径 sharpe 提取正则抛错时 metrics 为 None，结果仍正常返回。"""
    monkeypatch.setattr(loop_mod.re, "search", _raiser(RuntimeError, "regex broken"))
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        use_graph=True,
        graph=_Graph({"messages": [{"content": "sharpe 2.0"}]}),
        trace=False,
    )

    res = loop.run("q")
    assert res.metrics is None
    assert res.text == "sharpe 2.0"
    assert res.reason == "completed"


# ------------------------------------------------------------------ 补充：可达的相邻分支


def test_replay_directory_with_usage_sidecar_is_resolved(tmp_path):
    """回放路径是目录时自动定位其中的 llm_usage.json 并复用。"""
    replay_dir = tmp_path / "case"
    replay_dir.mkdir()
    (replay_dir / "llm_usage.json").write_text(
        json.dumps({"text": "from sidecar", "llm_usage": {"input_tokens": 2, "output_tokens": 3}}),
        encoding="utf-8",
    )
    loop = AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(replay_dir), allow_root=str(tmp_path), trace=False)

    res = loop.run("q")
    assert res.text == "from sidecar"
    assert res.metrics is None


def test_replay_chunks_are_joined_when_text_missing(tmp_path):
    """回放 JSON 无 text 但有 chunks 时按 dict.text/content 与纯字符串拼接。"""
    replay = tmp_path / "replays" / "chunks.json"
    replay.parent.mkdir(parents=True)
    replay.write_text(
        json.dumps({"chunks": [{"text": "A"}, {"content": "B"}, "C"], "llm_usage": {}}),
        encoding="utf-8",
    )
    loop = AgentLoop(
        llm=_ScriptedLLM([[]]),
        replay_path=str(replay),
        allow_root=str(replay.parent),
        trace=False,
    )

    res = loop.run("q")
    assert res.text == "ABC"


def test_grounding_float_retry_double_failure_marks_unverified():
    """字符串价格与 float 归一都失败时判定未接地，轮次耗尽收敛 grounding_failed。"""

    class _AlwaysRejectLedger:
        _evidence = {"AAPL": {"close": 100.0}}

        def __init__(self):
            self.calls = []

        def assert_price(self, symbol, price, authorized=None):
            self.calls.append((symbol, price))
            raise AssertionError(f"price {price} not in evidence")

    ledger = _AlwaysRejectLedger()
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 7.0"}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False).run("q")
    assert res.reason == "grounding_failed"
    assert res.grounding_verified is False
    # 每个数字至少尝试两次（原值 + float 归一），并含首 symbol 回退
    assert ("AAPL", "7.0") in ledger.calls
    assert ("AAPL", 7.0) in ledger.calls


def test_grounding_keyboard_interrupt_propagates():
    """接地校验内的 KeyboardInterrupt 必须透传，不得被 grounding_error 吞掉。"""

    class _InterruptLedger:
        _evidence = {"AAPL": {"close": 100.0}}

        def assert_price(self, symbol, price, authorized=None):
            raise KeyboardInterrupt

    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 100.0"}]])
    with pytest.raises(KeyboardInterrupt):
        AgentLoop(llm=llm, max_iterations=2, grounding=_InterruptLedger(), trace=False).run("q")


def test_grounding_error_trace_failure_is_swallowed(tmp_path):
    """grounding_error 落盘失败时仍按 fail-closed 收敛为 grounding_failed。"""

    class _IntKeyLedger:
        _evidence = {123: {"close": 100.0}}

        def assert_price(self, symbol, price, authorized=None):
            raise AssertionError("unreachable")

    writer = _BrokenWriter(str(tmp_path / "broken.jsonl"))
    llm = _ScriptedLLM([[{"type": "text", "text": "AAPL close 100"}]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=_IntKeyLedger(), trace=writer).run("q")
    assert res.reason == "grounding_failed"
    assert res.grounding_verified is False
    assert writer.attempts > 0


def test_tool_price_claim_failing_grounding_terminates_as_grounding_failed(monkeypatch):
    """工具成功但价格未接地：按纠正轮继续，轮次耗尽以 grounding_failed 终止。"""
    calls = []

    def _fn(**kwargs):
        calls.append(kwargs)
        # 批次18 P1-9：工具回显是数据、不是模型的价格断言，数字改由模型文本给出。
        return "AAPL close data"

    _register(monkeypatch, "wt_price_tool", _fn)
    ledger = GroundingLedger()
    ledger.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    llm = _ScriptedLLM([[
        {"type": "text", "text": "AAPL close 7"},
        {"type": "tool_call", "name": "wt_price_tool", "arguments": {}},
    ]])

    res = AgentLoop(llm=llm, max_iterations=2, grounding=ledger, trace=False).run("q")
    assert res.reason == "grounding_failed"
    assert res.grounding_verified is False
    assert len(calls) == 2


def test_budget_estimate_cost_failure_fails_closed(monkeypatch):
    """真实 usage 下 estimate_cost 抛异常必须 fail-visible 并熔断为 budget_fallback。"""

    class _Breaker:
        def record_usage(self, usage):
            return None

        def estimate_cost(self, usage):
            raise RuntimeError("pricing table broken")

    trace = _Recorder()
    chunk = {"type": "text", "text": "hi", "usage_metadata": {"input_tokens": 10, "output_tokens": 5}}

    res = AgentLoop(llm=_ScriptedLLM([[chunk]]), max_iterations=2, budget_breaker=_Breaker(), trace=trace).run("q")
    assert res.reason == "budget_fallback"
    assert "pricing table broken" in trace.first("budget_error")["error"]


def test_autofill_symbol_from_goal_and_backtest_default(monkeypatch):
    """goal 带 600519.SH 时回填；goal 无 symbol 的 run_backtest 回填默认 600519.SH。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "get_market_data", _fn)
    _register(monkeypatch, "run_backtest", _fn)
    chunks = [
        {"type": "tool_calls", "calls": [
            {"name": "get_market_data", "arguments": {}},
            {"name": "run_backtest", "arguments": {}},
        ]},
    ]
    res = AgentLoop(llm=_ScriptedLLM([chunks]), max_iterations=2, trace=False).run("分析 600519.SH")
    assert seen == [{"symbol": "600519.SH"}, {"symbol": "600519.SH"}]
    assert res.reason == "completed"


def test_metrics_sharpe_extracted_from_text():
    """成功文本内的 sharpe 被提取为 metrics（正则命中分支）。"""
    llm = _ScriptedLLM([[{"type": "text", "text": "result sharpe: 1.5"}]])
    res = AgentLoop(llm=llm, max_iterations=1, trace=False).run("q")
    assert res.metrics == {"sharpe": 1.5}


def test_estimate_tokens_none_returns_zero():
    """None 视为空输入，token 数为 0。"""
    assert estimate_tokens(None) == 0


def test_inject_none_hits_normalized_to_empty_list():
    """recall 返回 None 时归一化为空列表，不向上下文注入任何内容。"""

    class _Store:
        def recall(self, query):
            return None

    ctx = _Ctx()
    loop = AgentLoop(llm=_ScriptedLLM([[]]), memory_store=_Store(), context_manager=ctx, trace=False)
    assert loop.inject("q") == []
    assert ctx.added == []


def test_writeback_skips_empty_text():
    """空文本不触发写回（无内容可存），直接返回。"""

    class _Store:
        def write(self, key, content):
            raise AssertionError("should not be called")

    loop = AgentLoop(llm=_ScriptedLLM([[]]), memory_store=_Store(), trace=False)
    assert loop._writeback("") is None


def test_call_llm_without_supported_method_raises_attribute_error():
    """LLM 适配器没有任何约定方法时显式抛 AttributeError，不静默返回空流。"""
    loop = AgentLoop(llm=object(), trace=False)
    with pytest.raises(AttributeError, match="llm has no stream_chat/invoke/chat method"):
        loop._call_llm("q")


def test_normalize_stream_handles_none_dict_and_str():
    """流归一化：None -> []，dict -> [dict]，str -> text chunk，其它原样返回。"""
    loop = AgentLoop(llm=_ScriptedLLM([[]]), trace=False)
    assert loop._normalize_stream(None) == []
    assert loop._normalize_stream({"type": "text", "text": "d"}) == [{"type": "text", "text": "d"}]
    assert loop._normalize_stream("plain") == [{"type": "text", "text": "plain"}]
    chunks = [{"type": "text", "text": "x"}]
    assert loop._normalize_stream(chunks) is chunks


def test_tmp_replay_inner_relative_to_failure_stays_fail_closed(tmp_path, monkeypatch):
    """临时目录内层 relative_to 也失败时保持 fail-closed（不放行 /tmp 任意文件）。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "replays").mkdir()
    outside = _write_replay(tmp_path / "r.json", "leak")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HERO_ALLOW_TMP_REPLAY", "1")
    monkeypatch.setattr(Path, "is_relative_to", _raiser(AttributeError, "no is_relative_to"))
    monkeypatch.setattr(Path, "relative_to", _raiser(ValueError, "not relative"))

    with pytest.raises(ValueError, match="outside allowed directory"):
        AgentLoop(llm=_ScriptedLLM([[]]), replay_path=str(outside), trace=False)


def test_run_backtest_defaults_symbol_when_goal_has_none(monkeypatch):
    """goal 无任何 symbol 时 run_backtest 回填兜底 600519.SH。"""
    seen = []

    def _fn(**kwargs):
        seen.append(kwargs)
        return "ok"

    _register(monkeypatch, "run_backtest", _fn)
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "run_backtest", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("分析一下")
    assert seen == [{"symbol": "600519.SH"}]
    assert res.reason == "completed"


def test_tool_exception_is_wrapped_as_tool_error(monkeypatch):
    """工具函数抛普通异常时返回 (tool_error, exc)，不中断主循环。"""

    def _fn(**kwargs):
        raise RuntimeError("tool exploded")

    _register(monkeypatch, "wt_boom_tool", _fn)
    llm = _ScriptedLLM([[{"type": "tool_call", "name": "wt_boom_tool", "arguments": {}}]])

    res = AgentLoop(llm=llm, max_iterations=2, trace=False).run("q")
    assert "tool_error: tool exploded" in res.text
    assert res.reason == "max_iterations"
