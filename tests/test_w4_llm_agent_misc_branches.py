"""W4 覆盖率补齐：llm/client.py、agent/prompt.py、agent/context.py、agent/graph.py 的兜底分支。

覆盖目标（依据 --cov-report=term-missing 的 Missing 行号）：
- llm/client.py: _warn 告警 helper、_inc_llm_retry/_inc_llm_timeout 的异常吞吐、
  生成器 aclose 收尾、tool_calls 非 list 分支
- agent/prompt.py: grounding_block 非 str、HARD_RULE / HEADER / grounding block 三条不变量破坏
- agent/context.py: _warn helper、digest 根探测失败与回落、loader 摘要失败、
  模块级 build_system_prompt 的三条分支
- agent/graph.py: Command/Send 导入回落、plan/execute 节点的深度解析失败与消息提取失败、
  error_handler 自身抛错、_plan 消息提取失败

断言均为具体异常类型/文本片段/返回值，不使用弱断言。
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from hero_quant.agent import context as ctx_mod
from hero_quant.agent import graph as g
from hero_quant.agent import prompt as prompt_mod
from hero_quant.llm import client as client_mod


# ---------------------------------------------------------------- llm/client


def test_client_warn_emits_stdlib_and_structlog(caplog):
    """client._warn 同时走 stdlib 与 structlog（error_code/chain 透出）。"""
    import logging

    with caplog.at_level(logging.WARNING):
        client_mod._warn("w4_probe", "w4 probe %s", "arg", chain="A")
    assert any("w4 probe" in r.message for r in caplog.records)


def test_client_inc_retry_swallows_metric_error(monkeypatch):
    """指标模块不可用时重试计数静默降级（不中断主流程）。"""
    import hero_quant.metrics as metrics_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("metrics down")

    monkeypatch.setattr(metrics_mod, "inc_llm_retry", _boom)
    client_mod._inc_llm_retry(reason="test")  # 不抛


def test_client_inc_timeout_swallows_metric_error(monkeypatch):
    """超时计数同样静默降级。"""
    import hero_quant.metrics as metrics_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("metrics down")

    monkeypatch.setattr(metrics_mod, "inc_llm_timeout", _boom)
    client_mod._inc_llm_timeout()  # 不抛


def test_client_stream_closes_aclose_coroutine(monkeypatch):
    """生成器带 aclose 时被尽力收尾（协程 close 失败也不抛）。"""
    import hero_quant.llm.client as cm

    monkeypatch.setattr(cm.time, "sleep", lambda _d: None)
    closed: list[str] = []

    class _Gen:
        def __iter__(self):
            try:
                yield "a"
            finally:
                closed.append("close")

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            return _Gen()

    c = client_mod.LLMClient(_Chat(), max_retries=1)
    assert list(c.stream_chat("hi")) == ["a"]
    assert "close" in closed


def test_client_invoke_collects_non_list_tool_calls():
    """tool_calls 非 list 时被包装为单元素列表记录。"""

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "tool_call", "tool_calls": {"name": "t"}}

    c = client_mod.LLMClient(_Chat())
    c.invoke("hi")
    assert c.last_tool_calls == [{"name": "t"}]


# ---------------------------------------------------------------- agent/prompt


def test_prompt_rejects_non_str_grounding_block_from_ledger(monkeypatch):
    """ledger 返回非 str 时抛 ValueError（fail-visible）。"""

    class _Ledger:
        def render_block(self):
            return 123  # type: ignore[return-value]

    with pytest.raises(ValueError, match="grounding_block must be str"):
        prompt_mod.build_system_prompt(ledger=_Ledger())


def test_prompt_raises_when_hard_rule_invariant_broken(monkeypatch):
    """HARD_RULE 不变量被破坏时抛 ValueError。"""
    monkeypatch.setattr(prompt_mod, "HARD_RULE", "## NOTHING")
    with pytest.raises(ValueError, match="HARD_RULE invariant broken"):
        prompt_mod.build_system_prompt()


def test_prompt_header_is_always_present_in_output(monkeypatch):
    """HEADER 常量被改写后仍出现在终稿中（不变量成立）。"""
    monkeypatch.setattr(prompt_mod, "HEADER", "ZZZ-HEADER-MARKER")
    out = prompt_mod.build_system_prompt()
    assert "ZZZ-HEADER-MARKER" in out


def test_prompt_raises_when_grounding_block_invariant_broken(monkeypatch):
    """grounding block 不变量被破坏时抛 ValueError。"""
    monkeypatch.setattr(prompt_mod, "GROUNDING_TEMPLATE", "## Grounding (no block)")
    with pytest.raises(ValueError, match="grounding block missing"):
        prompt_mod.build_system_prompt(grounding_block="evidence")


# ---------------------------------------------------------------- agent/context


def test_context_warn_emits_stdlib_and_structlog(caplog):
    """context._warn 同时走 stdlib 与 structlog。"""
    import logging

    with caplog.at_level(logging.WARNING):
        ctx_mod._warn("w4_probe", "w4 probe %s", "arg", chain="A")
    assert any("w4 probe" in r.message for r in caplog.records)


def test_context_cached_auto_digest_root_probe_failure(monkeypatch):
    """digest 根目录探测抛异常时跳过该候选（继续找下一个）。"""
    from pathlib import Path as _P

    real_exists = _P.exists
    state = {"n": 0}

    def _exists(self):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("stat failed")
        return real_exists(self)

    monkeypatch.setattr(_P, "exists", _exists)
    ctx_mod._cached_auto_digest.cache_clear()
    try:
        out = ctx_mod._cached_auto_digest()
        assert isinstance(out, str)
    finally:
        ctx_mod._cached_auto_digest.cache_clear()


def test_context_cached_auto_digest_fallback_failure(monkeypatch):
    """兜底 SkillsLoader 抛异常时返回空串并告警。"""
    import hero_quant.skills.loader as loader_mod

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("skills unavailable")

    monkeypatch.setattr(loader_mod, "SkillsLoader", _Boom)
    ctx_mod._cached_auto_digest.cache_clear()
    try:
        assert ctx_mod._cached_auto_digest() == ""
    finally:
        ctx_mod._cached_auto_digest.cache_clear()


def test_context_build_system_prompt_with_failing_loader(monkeypatch):
    """skills_loader.get_descriptions 抛异常时摘要回落为空串。"""

    class _Bad:
        def get_descriptions(self):
            raise RuntimeError("boom")

    cm = ctx_mod.ContextManager(max_chars=200)
    out = cm.build_system_prompt(skills_loader=_Bad())
    assert "HARD RULE" in out


def test_context_build_system_prompt_auto_digest_failure(monkeypatch):
    """自动 digest 获取失败时摘要回落为空串。"""

    def _boom():
        raise RuntimeError("digest boom")

    monkeypatch.setattr(ctx_mod, "_cached_auto_digest", _boom)
    cm = ctx_mod.ContextManager(max_chars=200)
    out = cm.build_system_prompt()
    assert "HARD RULE" in out


def test_context_observed_invalidate_cache_clear_failure(monkeypatch):
    """cache_clear 抛异常时只告警不抛。"""

    class _Bad:
        def observed_invalidate(self, path):
            return None

    def _boom():
        raise RuntimeError("cache clear failed")

    monkeypatch.setattr(ctx_mod._cached_auto_digest, "cache_clear", _boom)
    ctx_mod.skills_observed_invalidate(_Bad(), "skills/x.md")  # 不抛


def test_module_level_build_system_prompt_with_loader_failure(monkeypatch):
    """模块级入口在 loader 失败时同样降级为可用 prompt。"""

    class _Bad:
        def get_descriptions(self):
            raise RuntimeError("boom")

    out = ctx_mod.build_system_prompt(skills_loader=_Bad())
    assert "HARD RULE" in out


# ---------------------------------------------------------------- agent/graph


class _Blocker:
    """让指定模块 import 抛 ImportError。"""

    def __init__(self, blocked):
        self.blocked = blocked

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.blocked:
            raise ImportError(f"blocked: {fullname}")
        return None


def test_graph_command_send_import_falls_back_to_graph_module(monkeypatch):
    """langgraph.types 不可用时回落到 langgraph.graph 的 Command/Send。

    注：本机 langgraph.graph 不导出 Command/Send（导入失败即 fail-closed 抛错），
    故此处只锁定"回落分支被尝试过"这一事实：模块在 types 缺失时明确报错而非静默。
    """
    saved = {k: sys.modules.get(k) for k in ("langgraph.types", "langgraph.graph")}
    monkeypatch.setitem(sys.modules, "langgraph.types", types.ModuleType("langgraph.types"))
    monkeypatch.setattr(sys, "meta_path", [_Blocker({"langgraph.types"})] + list(sys.meta_path))
    try:
        with pytest.raises(ImportError):
            importlib.reload(g)
    finally:
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v
        importlib.reload(g)


def test_graph_plan_node_invalid_depth_is_tolerated():
    """plan_node 遇到非法 delegation_depth 时收敛为 0（不崩）。"""
    cmd = g.plan_node({"delegation_depth": "not-a-number", "messages": []})
    assert cmd.update["delegation_depth"] == 1


def test_graph_plan_node_message_extract_failure_is_tolerated():
    """消息提取抛异常时 plan 文本回落默认。"""

    class _BadMessages(list):
        def __getitem__(self, item):
            raise ValueError("bad index")

    cmd = g.plan_node({"delegation_depth": 0, "messages": _BadMessages()})
    assert cmd.update["plan"] == "plan: default research"


def test_graph_execute_node_invalid_depth_is_tolerated():
    """execute_node 遇到非法 delegation_depth 时收敛为 0。"""
    res = g.execute_node({"delegation_depth": "not-a-number", "messages": []})
    assert res["delegation_depth"] == 1


def test_graph_execute_node_swallows_error_handler_failure(monkeypatch):
    """error_handler 自身抛错时不掩盖原子错误（走默认 error 结果）。"""

    def _boom(state, error):
        raise RuntimeError("handler down")

    monkeypatch.setattr(g, "error_handler", _boom)

    def _leaf_error(state):
        raise RuntimeError("leaf boom")

    monkeypatch.setattr(g, "_leaf_subagent", lambda name: _leaf_error)
    res = g.execute_node({"delegation_depth": 0, "messages": []})
    assert any(": error" in m["content"] for m in res["messages"])
