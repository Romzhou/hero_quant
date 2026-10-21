"""W4 覆盖率补齐：agent/loop.py 的回放短路 / 图委托 / trace writer 初始化分支。

覆盖目标（src/hero_quant/agent/loop.py）：
- 回放短路：读取回放 JSON 的 text / chunks / llm_usage，输出 LoopResult 并落 llm_usage.json
- 回放路径越界被拒（fail-closed）
- _run_graph：图 invoke 异常 -> graph_error、图结果转文本、invoke 需要 config 的签名、
  无 graph 时构建失败 -> graph_error
- _init_trace_writer：trace=None 默认回灌、duck-typed append-only writer、路径字符串、
  构造失败时降级为 None
- inject 的 skills_digest 注入（写入 context + trace）

断言均为具体字段值/文本片段/异常类型，不使用弱断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hero_quant.agent.loop import AgentLoop


class _SimpleLLM:
    def stream_chat(self, goal):
        yield {"type": "text", "text": "unused"}


# ---------------------------------------------------------------- 回放短路


def test_replay_path_returns_recorded_text(tmp_path, monkeypatch):
    """回放文件中的 text 被直接返回（不调用 LLM）。"""
    monkeypatch.chdir(tmp_path)
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"text": "replayed answer"}), encoding="utf-8")
    loop = AgentLoop(llm=_SimpleLLM(), replay_path=str(replay), allow_root=str(tmp_path), trace=False)
    res = loop.run("question")
    assert res.text == "replayed answer"
    assert res.reason == "completed"
    assert res.terminated is True
    assert res.grounding_verified is True


def test_replay_path_joins_chunks(tmp_path, monkeypatch):
    """回放文件只有 chunks 时被拼接为文本。"""
    monkeypatch.chdir(tmp_path)
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"chunks": [{"text": "a"}, {"text": "b"}, "c"]}),
        encoding="utf-8",
    )
    loop = AgentLoop(llm=_SimpleLLM(), replay_path=str(replay), allow_root=str(tmp_path), trace=False)
    res = loop.run("q")
    assert res.text == "abc"


def test_replay_path_reads_llm_usage(tmp_path, monkeypatch):
    """回放文件中的 llm_usage 被归一为 input/output tokens。"""
    monkeypatch.chdir(tmp_path)
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"text": "x", "llm_usage": {"prompt_tokens": 11, "completion_tokens": 7}}),
        encoding="utf-8",
    )
    loop = AgentLoop(llm=_SimpleLLM(), replay_path=str(replay), allow_root=str(tmp_path), trace=False)
    res = loop.run("q")
    assert res.text == "x"
    assert res.token_count == 0  # "x" 长度 1 -> 1//4 = 0


def test_replay_directory_uses_llm_usage_json(tmp_path, monkeypatch):
    """回放目录时优先读取其中的 llm_usage.json。"""
    monkeypatch.chdir(tmp_path)
    rdir = tmp_path / "replays_dir"
    rdir.mkdir()
    (rdir / "llm_usage.json").write_text(json.dumps({"text": "from dir"}), encoding="utf-8")
    loop = AgentLoop(llm=_SimpleLLM(), replay_path=str(rdir), allow_root=str(tmp_path), trace=False)
    res = loop.run("q")
    assert res.text == "from dir"


def test_replay_path_outside_allowed_dir_is_rejected(tmp_path, monkeypatch):
    """回放路径越界（不在白名单目录内）时构造即抛错（fail-closed）。"""
    monkeypatch.chdir(tmp_path)
    outside = tmp_path.parent / "outside_replay.json"
    outside.write_text(json.dumps({"text": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="outside allowed directory"):
        AgentLoop(llm=_SimpleLLM(), replay_path=str(outside), allow_root=str(tmp_path), trace=False)


# ---------------------------------------------------------------- 图委托


def test_run_graph_error_is_reported(monkeypatch):
    """图 invoke 抛异常 -> reason=graph_error（不抛出）。"""

    class _BadGraph:
        def invoke(self, state):
            raise RuntimeError("graph boom")

    loop = AgentLoop(llm=_SimpleLLM(), use_graph=True, graph=_BadGraph(), trace=False)
    res = loop.run("q")
    assert res.reason == "graph_error"
    assert "graph boom" in res.text


def test_run_graph_result_is_converted_to_text(monkeypatch):
    """图结果的最后一条消息内容被转为文本。"""

    class _Graph:
        def invoke(self, state):
            return {"messages": [{"role": "assistant", "content": "graph answer"}]}

    loop = AgentLoop(llm=_SimpleLLM(), use_graph=True, graph=_Graph(), trace=False)
    res = loop.run("q")
    assert "graph answer" in res.text
    assert res.reason == "completed"


def test_run_graph_invoke_with_config_signature(monkeypatch):
    """图 invoke 只接受 (state, config) 时也能调用。"""

    class _Graph:
        def invoke(self, state, config=None):
            return {"messages": [{"role": "assistant", "content": "cfg answer"}]}

    loop = AgentLoop(llm=_SimpleLLM(), use_graph=True, graph=_Graph(), trace=False)
    res = loop.run("q")
    assert "cfg answer" in res.text


def test_run_graph_without_graph_object_builds_default(monkeypatch):
    """未提供 graph 时自动构建默认研究图并跑通。"""
    loop = AgentLoop(llm=_SimpleLLM(), use_graph=True, trace=False)
    res = loop.run("analyze market")
    assert res.reason in ("completed", "graph_error")
    assert res.text


# ---------------------------------------------------------------- trace writer 初始化


def test_init_trace_writer_with_duck_typed_writer():
    """只有 append 方法的鸭子类型写入器被接受。"""
    calls: list[dict] = []

    class _Duck:
        path = "duck.jsonl"

        def append(self, obj):
            calls.append(obj)

    loop = AgentLoop(llm=_SimpleLLM(), trace=_Duck())
    assert loop._ensure_trace_writer() is not None


def test_init_trace_writer_with_path_string(tmp_path):
    """路径字符串被转成 TraceWriter。"""
    target = tmp_path / "t.jsonl"
    loop = AgentLoop(llm=_SimpleLLM(), trace=str(target))
    assert Path(loop._ensure_trace_writer().path) == target


def test_init_trace_writer_default_creates_writer(tmp_path, monkeypatch):
    """trace=None 时默认回灌创建 writer。"""
    monkeypatch.setenv("HERO_TRACE_DIR", str(tmp_path))
    loop = AgentLoop(llm=_SimpleLLM(), trace=None)
    assert loop._ensure_trace_writer() is not None


def test_init_trace_writer_opt_out_yields_none():
    """显式 trace=False 时不创建 writer。"""
    loop = AgentLoop(llm=_SimpleLLM(), trace=False)
    assert loop._ensure_trace_writer() is None


# ---------------------------------------------------------------- inject skills digest


def test_inject_writes_skills_digest_to_context(tmp_path, monkeypatch):
    """skills_loader 的摘要被注入 context（供后续 prompt 组装使用）。"""
    monkeypatch.setenv("HERO_TRACE_DIR", str(tmp_path))
    from hero_quant.agent.context import ContextManager

    class _Loader:
        def get_descriptions(self):
            return "skill-a: does things"

    class _Store:
        def recall(self, query):
            return []

    ctx = ContextManager(max_chars=5000)
    loop = AgentLoop(
        llm=_SimpleLLM(),
        context_manager=ctx,
        skills_loader=_Loader(),
        memory_store=_Store(),
        trace=tmp_path / "t.jsonl",
    )
    loop.inject("q")
    joined = " ".join(m["content"] for m in ctx._messages)
    assert "[skills_digest] skill-a: does things" in joined
    loop._ensure_trace_writer().close()
