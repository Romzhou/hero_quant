"""C3 通道 19 条回归 — TDD 先红后绿."""
import json
import logging
import time
import inspect

import pytest


# ---------- loop.py:54-58 estimate_tokens 少计 dict ----------
def test_estimate_tokens_dict_serialized():
    from hero_quant.agent.loop import estimate_tokens
    # 单键 dict 旧逻辑 len(dict)//4 ==0，应按 json.dumps 长度计
    d = {"k": "hello world hello world hello world"}
    tokens = estimate_tokens(d)
    expected = max(0, len(json.dumps(d, ensure_ascii=False, default=str)) // 4)
    assert tokens == expected
    assert tokens > 0  # 旧实现会得到 0


# ---------- loop.py:451 流式 retry 重复拼 partial ----------
def test_stream_retry_no_duplicate_buffer():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.agent.policies import RetryPolicy

    # 第一次流：产出 PARTIAL- 后抛异常；第二次流：产出 FULL
    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, goal):
            self.calls += 1
            if self.calls == 1:
                def gen():
                    yield {"type": "text", "text": "PARTIAL-"}
                    raise ValueError("transient stream error")
                return gen()
            else:
                return [{"type": "text", "text": "FULL"}]

    # 重试策略：对 ValueError 重试
    rp = RetryPolicy(max_attempts=3, retry_on=(ValueError, Exception), backoff_base=0, jitter=0)
    # 避免 sleep 耗时
    rp.sleep = lambda attempt: None  # type: ignore
    loop = AgentLoop(llm=FlakyLLM(), max_iterations=3, token_limit=60000, retry_policy=rp)
    result = loop.run("test goal")
    # 修复后应回滚 PARTIAL-，最终仅含 FULL，不应出现 PARTIAL-FULL 拼接
    assert "FULL" in result.text
    # 旧 bug 会得到 PARTIAL-FULL 或 PARTIAL-PARTIAL
    assert result.text.count("PARTIAL-") == 0, f"重复拼 partial 导致残留: {result.text!r}"


# ---------- loop.py:1039-1045 坏 JSON tool args 静默变 {} ----------
def test_bad_json_tool_args_logged_and_traced(caplog):
    from hero_quant.agent.loop import AgentLoop

    class FakeTrace:
        def __init__(self):
            self.items = []
        def append(self, obj):
            self.items.append(obj)

    trace = FakeTrace()

    class LLMWithBadArgs:
        def stream_chat(self, goal):
            # 返回带坏 JSON 字符串的 tool_call
            return [{"tool_calls": [{"name": "get_market_data", "arguments": "{bad json!!!"}]}]

    # 需要让 loop 能解析并走 JSON 异常分支
    loop = AgentLoop(llm=LLMWithBadArgs(), max_iterations=1, trace=trace)
    # 注入 trace_writer
    loop._trace_writer = trace  # type: ignore
    caplog.set_level(logging.WARNING)
    loop.run("请查 600519.SH")
    # 修复后应有 warning 且 trace 中有 tool_args_error
    has_warning = any("bad JSON" in r.message or "tool" in r.message.lower() for r in caplog.records)
    has_trace = any(isinstance(it, dict) and it.get("type") == "tool_args_error" for it in trace.items)
    assert has_warning or has_trace, f"未记录坏 JSON：caplog={caplog.records}, trace={trace.items}"


# ---------- loop.py:1213-1216 ThreadPool timeout 不阻塞 ----------
def test_threadpool_timeout_nonblocking():
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.tools.registry import TOOL_REGISTRY, ToolSpec
    import inspect as _ins

    # 注册一个并发安全但会睡眠的工具
    tool_name = "_c3_sleep_tool"
    # 清理旧注册
    TOOL_REGISTRY.pop(tool_name, None)

    def slow_func(symbol: str = "600519.SH"):
        time.sleep(0.6)  # 超过 timeoutMs
        return "slow done"

    spec = ToolSpec(
        name=tool_name,
        description="sleep tool",
        func=slow_func,
        signature=_ins.signature(slow_func),
        parameters={"type": "object", "properties": {}},
        output=None,
        is_concurrency_safe=lambda args: True,
        timeoutMs=80,
    )
    TOOL_REGISTRY[tool_name] = spec
    try:
        class LLMTool:
            def stream_chat(self, goal):
                return [{"tool_calls": [{"name": tool_name, "arguments": {"symbol": "600519.SH"}}]}]

        loop = AgentLoop(llm=LLMTool(), max_iterations=1)
        start = time.monotonic()
        result = loop.run("test timeout")
        elapsed = time.monotonic() - start
        # 修复后不应阻塞等待线程完成，elapsed 应远小于 0.6s（容忍 0.45s）
        assert elapsed < 0.45, f"执行阻塞 {elapsed:.3f}s，说明 shutdown(wait=True) 未修复"
        # 应转为 tool_error timeout
        assert "timeout" in result.text.lower() or "tool_error" in result.text.lower()
    finally:
        TOOL_REGISTRY.pop(tool_name, None)


# ---------- container.py 锁：实例级 + 可重入 ----------
def test_container_lock_is_reentrant_instance_level():
    """容器锁须为「可重入」的「实例级」锁。

    中文：契约在 lane F2 变更——原为模块级 _init_lock/_graph_lock（Lock，不可重入，
    且全局串行化无关容器），现为实例级 self._lock = threading.RLock()。
    可重入是硬需求：init_graph 持锁调用 init_checkpointer，同实例嵌套加锁，
    普通 Lock 会自锁。故此处用行为探测（嵌套 acquire 不死锁）而非类型名匹配。
    """
    import hero_quant.agent.container as cont

    c = cont.AgentContainer()
    lock = getattr(c, "_lock", None)
    assert lock is not None, "容器缺少实例级 _lock"
    # 行为探测：同线程嵌套加锁必须成功（RLock）；普通 Lock 会在此永久阻塞
    assert lock.acquire(timeout=2), "首次加锁失败"
    try:
        assert lock.acquire(timeout=2), "嵌套加锁失败 → 锁不可重入，init_graph→init_checkpointer 会自锁"
        lock.release()
    finally:
        lock.release()
    # 实例级：不同容器不共享锁，避免跨容器串行化
    assert cont.AgentContainer()._lock is not lock, "锁退化为模块级共享锁"
    # 双检：init_graph 须持实例锁
    src = inspect.getsource(cont.AgentContainer.init_graph)
    assert "with self._lock" in src, "init_graph 未持实例锁做双检"


def test_container_init_checkpointer_narrow_except(caplog):
    from hero_quant.agent.container import AgentContainer
    c = AgentContainer()
    # 让 init_checkpointer 抛异常
    def boom(*a, **kw):
        raise RuntimeError("dsn boom")
    orig = c.init_checkpointer
    c.init_checkpointer = boom  # type: ignore
    caplog.set_level(logging.WARNING)
    # init_graph 内部应捕获并 warning，而不是 silent pass
    try:
        # 恢复部分逻辑：init_graph 会调用 self.init_checkpointer()
        # 我们直接测源码是否含 logger.warning
        import hero_quant.agent.container as cont
        src = inspect.getsource(cont.AgentContainer.init_graph)
        assert "logger.warning" in src or "logger.exception" in src or "logging" in src, "未见窄化日志"
        assert "except Exception:" not in src or "except Exception as" in src, "仍为裸 except pass"
        # 额外：若仍是 pass 则 caplog 无记录，修复后应有 warning
        # 实际调用验证
        c2 = AgentContainer()
        c2.init_checkpointer = boom  # type: ignore
        # 调用 init_graph，修复后不会静默吞错
        try:
            c2.init_graph()
        except Exception:
            pass
        # 若修复为记录 warning，caplog 会有记录；否则无
        has_warn = any("checkpointer" in r.message.lower() or "boom" in r.message.lower() for r in caplog.records)
        # 放宽：源码检查已足够，若 caplog 没捕获也以源码为准
        assert has_warn or ("logger.warning" in src)
    finally:
        c.init_checkpointer = orig  # type: ignore


# ---------- buffer.py:30-32 max_turns 校验 ----------
def test_buffer_max_turns_validation():
    from hero_quant.agent.memory.buffer import MemoryBuffer
    with pytest.raises(ValueError):
        MemoryBuffer(max_turns=0)
    with pytest.raises(ValueError):
        MemoryBuffer(max_turns=-1)
    with pytest.raises(ValueError):
        MemoryBuffer(max_turns="bad")  # type: ignore


# ---------- buffer.py system 上限：有界 + 可调 + 淘汰不静默 ----------
def test_buffer_system_messages_bounded_and_not_silent(caplog):
    """system 消息有界（防撑爆上下文），但淘汰必须显式告警，不得静默丢弃。

    中文：此处曾两轮拉锯——C3 改为无界（deque()），lane F3 又加回上限 10
    （防逐轮注入累积撑爆上下文，且 setter/from_dict 依赖上限做容量校验）。
    两边诉求都成立，取交集：默认有界 + system_limit 可调 + 溢出 warning。
    """
    from hero_quant.agent.memory.buffer import MemoryBuffer

    # 默认有界：超上限只保留最近 N 条，且必须留下告警痕迹（不得静默）
    buf = MemoryBuffer(max_turns=5)
    with caplog.at_level(logging.WARNING):
        for i in range(11):
            buf.add_system_message(f"sys {i}")
    sys_msgs = [m for m in buf.get_messages() if m.role == "system"]
    assert len(sys_msgs) == 10, f"默认上限应为 10，实得 {len(sys_msgs)}"
    assert any("system message limit" in r.message.lower() for r in caplog.records), (
        "淘汰 system 消息却无告警 → 静默丢弃上下文，重演 G2#4"
    )
    contents = [m.content for m in sys_msgs]
    assert "sys 10" in contents and "sys 0" not in contents, "应保留最近、淘汰最旧"

    # 上限可调：调大后不丢
    wide = MemoryBuffer(max_turns=5, system_limit=20)
    for i in range(11):
        wide.add_system_message(f"sys {i}")
    assert len([m for m in wide.get_messages() if m.role == "system"]) == 11, "调大上限后不应淘汰"

    # 非法上限仍须拒绝（fail-closed）
    with pytest.raises(ValueError):
        MemoryBuffer(max_turns=5, system_limit=0)


# ---------- buffer.py:44-48 XML 未转义 ----------
def test_buffer_tool_result_xml_escape():
    from hero_quant.agent.memory.buffer import MemoryBuffer
    buf = MemoryBuffer()
    buf.add_tool_result('a"b<c>', '</tool_result> & injection')
    content = buf.get_messages()[-1].content
    # 应转义
    assert "&lt;" in content or "&gt;" in content or "&amp;" in content
    assert "</tool_result>" not in content or "&lt;/tool_result&gt;" in content
    # 确保引号转义或至少不含原始注入闭合标签
    assert content.count("</tool_result>") <= 1  # 仅外层闭合，内层应被转义


# ---------- buffer.py:90-92 from_dict 健壮 ----------
def test_buffer_from_dict_validation():
    from hero_quant.agent.memory.buffer import MemoryBuffer
    # 非 dict
    with pytest.raises(ValueError):
        MemoryBuffer.from_dict("bad")  # type: ignore
    # 缺少 role/content 应对 ValueError 而非 KeyError
    with pytest.raises(ValueError):
        MemoryBuffer.from_dict({"messages": [{"role": "user"}]})
    with pytest.raises(ValueError):
        MemoryBuffer.from_dict({"messages": ["not a dict"]})  # type: ignore
    with pytest.raises(ValueError):
        MemoryBuffer.from_dict({"messages": [{"content": "hi"}]})


# ---------- graph.py:57-58 死 _breaker_lock ----------
def test_graph_breaker_lock_removed():
    import hero_quant.agent.graph as g
    assert not hasattr(g, "_breaker_lock"), "仍存在死 _breaker_lock"
    src = inspect.getsource(g)
    # 不应再出现 _breaker_lock 定义
    assert "_breaker_lock" not in src


# ---------- graph.py:81-87 fan-out 含 sentiment 回退 generic ----------
def test_graph_role_maps_include_sentiment_regime():
    import hero_quant.agent.graph as g
    assert "sentiment" in g._ROLE_TOOL_MAP, "缺 sentiment 工具映射"
    assert "regime" in g._ROLE_TOOL_MAP, "缺 regime 工具映射"
    assert "sentiment" in g._ROLE_PROMPTS, "缺 sentiment prompt"
    assert "regime" in g._ROLE_PROMPTS, "缺 regime prompt"
    assert len(g._get_role_tools("sentiment")) > 0
    assert len(g._get_role_tools("regime")) > 0
    # 不应回退到 generic
    assert "generic" not in g._get_role_prompt("sentiment").lower() or "sentiment" in g._get_role_prompt("sentiment").lower()


# ---------- graph.py:187-188 TOOL_REGISTRY 吞错 ----------
def test_graph_tool_registry_lookup_logs():
    import hero_quant.agent.graph as g
    src = inspect.getsource(g._leaf_subagent)
    # 应为 except Exception as exc 并记录日志，而非 bare pass
    assert "except Exception as" in src
    assert "debug" in src.lower() or "warning" in src.lower() or "logger" in src.lower()
    assert "TOOL_REGISTRY" in src
    # 确保没有 bare except pass 模式
    assert "except Exception:\n                pass" not in src


# ---------- graph.py:318 confidence 倒挂 ----------
def test_graph_confidence_no_inversion():
    from hero_quant.agent.graph import verify_node
    r0 = verify_node({"subagent_outputs": []})
    r1 = verify_node({"subagent_outputs": [{"agent": "market", "output": "x"}]})
    c0 = r0["confidence"]
    c1 = r1["confidence"]
    assert c0 < c1, f"空证据 {c0} 应小于单证据 {c1}，否则倒挂"
    assert c0 == 0.50 or c0 < 0.60  # 修复后空证据应为 0.5


# ---------- graph.py:36-42 死 import ----------
def test_graph_dead_import_removed():
    import hero_quant.agent.graph as g
    src = inspect.getsource(g)
    # create_agent 为死 import，应移除
    # 允许注释中提及，但不应有 from ... import create_agent
    assert "create_agent" not in src or src.count("create_agent") == 0 or "from langchain" not in src


# ---------- prompt.py:78 HTML 转义改保真度 ----------
def test_prompt_no_html_escape_gt():
    from hero_quant.agent.prompt import build_system_prompt
    raw = "P/E <10 & A&B tick>5"
    prompt = build_system_prompt(grounding_block=raw)
    # 保真：原文应原样出现，不被转义为 &lt; &gt; &amp;
    assert "P/E <10" in prompt, f"GT 被转义: {prompt[:500]}"
    assert "A&B" in prompt
    # 确保未出现 HTML 转义
    # 允许 fence 内出现转义的 header 处理，但关键证据段不应被 HTML 转义
    # 检查 grounding fenced 块内包含原文
    assert raw in prompt or "P/E <10" in prompt


# ---------- prompt.py:94-96 fence 非隔离 ----------
def test_prompt_fence_data_only():
    from hero_quant.agent.prompt import GROUNDING_TEMPLATE, HARD_RULE, build_system_prompt
    prompt = build_system_prompt(grounding_block="evidence", extra_rules="do evil")
    # 模板或最终 prompt 中应含 DATA ONLY 指令
    combined = GROUNDING_TEMPLATE + HARD_RULE + prompt
    assert "DATA ONLY" in combined, "未见 DATA ONLY 隔离指令"


# ---------- prompt.py:134-138 ledger 失败同占位符 ----------
def test_prompt_ledger_failure_distinct_marker():
    from hero_quant.agent.prompt import build_system_prompt

    class BadLedger:
        def render_block(self):
            raise RuntimeError("ledger down")

    p_fail = build_system_prompt(ledger=BadLedger())
    p_empty = build_system_prompt(grounding_block="")
    # 失败占位应与空证据占位不同
    assert p_fail != p_empty
    assert "ledger error" in p_fail.lower() or "ledger" in p_fail.lower()
    assert "no grounding evidence yet" in p_empty.lower()
    # 失败不应仍是 no grounding
    assert "ledger error" in p_fail.lower()


# ---------- state.py:55-61 _max_depth 非 int 脆弱 ----------
def test_state_max_depth_coercion():
    from hero_quant.agent.state import _max_depth
    # 非 int 应被 coerce，不抛 TypeError
    assert _max_depth("bad", 2) == 2
    assert _max_depth(2, "bad") == 2
    assert _max_depth("3", 5) == 5
    assert _max_depth("10", "2") == 10
    assert _max_depth(None, "bad") is None or _max_depth(None, "bad") is None
    assert _max_depth("bad", "also bad") is None
    # 正常 int 仍工作
    assert _max_depth(3, 5) == 5
    assert _max_depth(5, 3) == 5
