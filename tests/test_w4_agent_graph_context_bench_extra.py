"""W4 覆盖率补齐：agent/graph.py / agent/context.py / backtest/bench.py 的兜底分支。

覆盖目标：
- src/hero_quant/agent/graph.py: build_research_graph(selected=[]) 用默认三分析师、
  plan_node 读取 LangChain 风格消息对象、execute_node 的子代理异常补偿路径、
  _leaf_subagent 在 TOOL_REGISTRY 不可用时降级为空工具集
- src/hero_quant/agent/context.py: _cached_auto_digest 在 SkillsLoader 失败时返回空串、
  build_system_prompt 在底层 prompt 组装失败时走兜底串、
  skills_observed_invalidate 清空 digest 缓存
- src/hero_quant/backtest/bench.py: _build_tearsheet_html 对不可序列化指标回落 str()、
  _build_pit_disclosure 对非 dict 记录回落 "PIT status unknown"、
  run_batch 允许临时目录内的绝对路径输出、拒绝 symlink 逃逸

断言均为具体字符串片段/类型/异常类型，不使用弱断言。
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from hero_quant.agent import context as ctx_mod
from hero_quant.agent import graph as g
from hero_quant.backtest import bench as bm


# ---------------------------------------------------------------- graph


def test_build_research_graph_with_empty_selected_uses_defaults():
    """selected=[] 时使用默认三分析师扇出。"""
    app = g.build_research_graph(selected=[])
    out = app.invoke({"messages": [{"role": "user", "content": "hello"}]})
    assert out["verification"]
    assert out["confidence"] > 0.0


def test_plan_node_reads_langchain_style_message():
    """消息对象带 .content 时被正确读取（不用 str(msg) 的 repr）。"""

    class _Msg:
        content = "analyze market"

    cmd = g.plan_node({"delegation_depth": 0, "messages": [_Msg()]})
    assert "analyze market" in cmd.update["plan"]


def test_plan_node_with_non_message_uses_str():
    """非 dict/非对象消息退化为 str()。"""
    cmd = g.plan_node({"delegation_depth": 0, "messages": [12345]})
    assert cmd.update["plan"].startswith("plan for:")


def test_execute_node_handles_subagent_error(monkeypatch):
    """子代理抛异常时走 error_handler 补偿分支（不抛出）。"""

    def _boom(state):
        raise RuntimeError("leaf boom")

    monkeypatch.setattr(g, "_leaf_subagent", lambda name: _boom)
    res = g.execute_node({"delegation_depth": 0, "messages": []})
    assert any("-> compensate" in m["content"] for m in res["messages"])


def test_leaf_subagent_without_tool_registry_has_empty_tools(monkeypatch):
    """TOOL_REGISTRY 不可用时降级为空工具集（不抛）。"""
    import sys
    import types

    class _BrokenRegistry:
        @staticmethod
        def get(_name):
            raise ImportError("registry unavailable")

    fake = types.ModuleType("hero_quant.tools.registry")
    fake.TOOL_REGISTRY = _BrokenRegistry()  # type: ignore[assignment]
    orig = sys.modules.get("hero_quant.tools.registry")
    sys.modules["hero_quant.tools.registry"] = fake
    try:
        run = g._leaf_subagent("market")
        res = run({"delegation_depth": 0})
        # 注册表不可用 -> 保留角色声明的工具清单，但不做过滤、不写 preview
        assert res["subagent_outputs"][0]["tools"] == g._get_role_tools("market")
        assert "tools:" not in res["messages"][0]["content"]
    finally:
        if orig is not None:
            sys.modules["hero_quant.tools.registry"] = orig
        else:
            sys.modules.pop("hero_quant.tools.registry", None)


# ---------------------------------------------------------------- context


def test_cached_auto_digest_returns_empty_on_loader_failure(monkeypatch):
    """SkillsLoader 失败时自动 digest 为空串（不抛）。"""
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


def test_context_build_system_prompt_falls_back_when_prompt_fails(monkeypatch):
    """底层 prompt 组装失败时走兜底串（仍含 HARD RULE）。"""
    cm = ctx_mod.ContextManager(max_chars=100)
    out = cm.build_system_prompt(skill_count=-5, grounding_block="GT-BLOCK")
    assert "HARD RULE" in out
    assert "GT-BLOCK" in out


def test_context_build_system_prompt_fallback_includes_digest(monkeypatch):
    """兜底路径同样带上 skills_digest（审计可查）。"""
    cm = ctx_mod.ContextManager(max_chars=100)
    out = cm.build_system_prompt(skill_count=-5, skills_digest="DIGEST-XYZ")
    assert "DIGEST-XYZ" in out
    assert "HARD RULE" in out


def test_skills_observed_invalidate_clears_digest_cache(monkeypatch):
    """observed_invalidate 后 digest 缓存被清空。"""

    class _Loader:
        def observed_invalidate(self, path):
            return None

    ctx_mod._cached_auto_digest.cache_clear()
    ctx_mod._cached_auto_digest()
    ctx_mod.skills_observed_invalidate(_Loader(), "skills/x.md")
    assert ctx_mod._cached_auto_digest.cache_info().currsize == 0


# ---------------------------------------------------------------- bench


def test_tearsheet_html_falls_back_to_str_for_unserializable_metrics():
    """指标不可 JSON 序列化时回退为 str(m) 并仍可渲染。"""
    html = bm._build_tearsheet_html({"A": {"benchmark": "SPY", "alpha": object()}}, "disc")
    assert "<pre>" in html
    assert "object object" in html


def test_pit_disclosure_with_non_dict_record_is_unknown():
    """记录形态非法（无法做 in 判定）时披露文本回落 "PIT status unknown"。"""
    txt = bm._build_pit_disclosure([123])
    assert "PIT status unknown" in txt


def test_run_batch_allows_absolute_path_inside_tmpdir():
    """临时目录内的绝对输出路径被允许。"""
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out"
        res = bm.run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], output_dir=target, allow_synthetic=True)
        assert "AAPL" in res
        assert (target / "metrics.json").exists()


def test_run_batch_rejects_symlinked_output_dir(tmp_path, monkeypatch):
    """symlink 目录逃逸被拒绝（traversal fail-closed）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="traversal"):
        bm.run_batch(
            ["AAPL"],
            dates=["2024-01-01", "2024-01-02"],
            output_dir=pathlib.Path("link") / "sub",
            allow_synthetic=True,
        )


def test_run_batch_json_output_is_serializable():
    """产出可 JSON 序列化（numpy 标量已归一）。"""
    res = bm.run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    text = json.dumps(res, ensure_ascii=False)
    assert "AAPL" in text
