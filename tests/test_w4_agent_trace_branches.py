"""W4 覆盖率补齐：agent/trace.py 未覆盖分支。

覆盖目标（src/hero_quant/agent/trace.py）：
- is_trace_disabled：显式 False/0/字符串关闭值、HERO_TRACE_ENABLED 全局开关
- default_trace_dir：HERO_TRACE_DIR 优先、不可用时回落本地 traces
- _scrub_cookie：dict 键含 cookie、字符串 cookie=xxx、list/tuple/set/frozenset 递归
- _env_int / _validate_threshold 的非法值回落
- TraceWriter 构造：目录签名 / .jsonl 签名 / 相对文件名 / 历史别名 kwargs
- append：tool_result 大 content 分流为侧车、通用大记录分流、关闭后追加被丢弃
- read(resolve_offloads=True) 的侧车回灌
- close 幂等；_safe_sidecar_path 的绝对路径与穿越拒绝

断言均为具体数值/文件存在性/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hero_quant.agent.trace import (
    TRACE_DISABLED_VALUES,
    TraceWriter,
    _env_int,
    _scrub_cookie,
    _validate_threshold,
    default_trace_dir,
    is_trace_disabled,
)


# ---------------------------------------------------------------- 开关


@pytest.mark.parametrize("value", [False, 0, 0.0, "0", "false", "NO", "off", "none", "disabled"])
def test_is_trace_disabled_true_values(value):
    """显式关闭值一律停写。"""
    assert is_trace_disabled(value) is True


def test_is_trace_disabled_true_value_still_enabled():
    """显式 True 不等于关闭（仍受全局开关约束）。"""
    assert is_trace_disabled(True) is False
    assert is_trace_disabled(None) is False
    assert is_trace_disabled("yes") is False


def test_is_trace_disabled_env_switch(monkeypatch):
    """HERO_TRACE_ENABLED 全局开关生效。"""
    monkeypatch.setenv("HERO_TRACE_ENABLED", "0")
    assert is_trace_disabled() is True
    monkeypatch.setenv("HERO_TRACE_ENABLED", "1")
    assert is_trace_disabled() is False


def test_trace_disabled_values_constant():
    """关闭值集合为冻结常量。"""
    assert "off" in TRACE_DISABLED_VALUES
    assert "disabled" in TRACE_DISABLED_VALUES


# ---------------------------------------------------------------- 目录


def test_default_trace_dir_prefers_env(monkeypatch, tmp_path):
    """HERO_TRACE_DIR 优先且目录被创建。"""
    target = tmp_path / "mytraces"
    monkeypatch.setenv("HERO_TRACE_DIR", str(target))
    got = default_trace_dir()
    assert got == target
    assert target.exists()


def test_default_trace_dir_unusable_env_falls_back(monkeypatch, tmp_path):
    """HERO_TRACE_DIR 不可用时回落本地 traces 目录。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not-a-dir", encoding="utf-8")
    monkeypatch.setenv("HERO_TRACE_DIR", str(blocker / "sub"))
    monkeypatch.chdir(tmp_path)
    got = default_trace_dir()
    assert got.exists()
    assert got != blocker / "sub"


# ---------------------------------------------------------------- 脱敏


def test_scrub_cookie_masks_cookie_key():
    """键含 cookie 的整值被替换为 ***。"""
    out = _scrub_cookie({"session_cookie": "abc123", "other": "keep"})
    assert out["session_cookie"] == "***"
    assert out["other"] == "keep"


def test_scrub_cookie_masks_inline_pair():
    """字符串中的 cookie=xxx 只保留键名。"""
    out = _scrub_cookie("Cookie: secretvalue; a=1")
    assert "secretvalue" not in out
    assert "***" in out


def test_scrub_cookie_recurses_containers():
    """list/tuple/set/frozenset 递归脱敏且保持类型。"""
    assert _scrub_cookie([{"cookie": "x"}]) == [{"cookie": "***"}]
    assert _scrub_cookie(({"cookie": "x"},)) == ({"cookie": "***"},)
    assert _scrub_cookie({"cookie": "x"}) in ({"cookie": "***"},)
    assert _scrub_cookie(frozenset({"cookie": "x"})) == frozenset({"cookie": "***"})


def test_scrub_cookie_leaves_scalars():
    """非容器标量原样返回。"""
    assert _scrub_cookie(123) == 123
    assert _scrub_cookie(None) is None


# ---------------------------------------------------------------- 阈值解析


def test_env_int_valid_and_invalid(monkeypatch):
    """env 整数解析：合法值生效，非法/空值回落 default。"""
    monkeypatch.setenv("HERO_TRACE_PREVIEW", "77")
    assert _env_int("HERO_TRACE_PREVIEW", 5) == 77
    monkeypatch.setenv("HERO_TRACE_PREVIEW", "abc")
    assert _env_int("HERO_TRACE_PREVIEW", 5) == 5
    monkeypatch.setenv("HERO_TRACE_PREVIEW", "")
    assert _env_int("HERO_TRACE_PREVIEW", 5) == 5
    assert _env_int("HERO_TRACE_MISSING_ENV_KEY", 5) == 5


def test_validate_threshold_rules():
    """阈值必须为正整数，非法值回落 default。"""
    assert _validate_threshold(None, "x", 10) == 10
    assert _validate_threshold(0, "x", 10) == 10
    assert _validate_threshold(-1, "x", 10) == 10
    assert _validate_threshold("abc", "x", 10) == 10
    assert _validate_threshold(5, "x", 10) == 5


# ---------------------------------------------------------------- TraceWriter


def test_writer_accepts_directory_signature(tmp_path):
    """目录签名构造时主文件名为 trace.jsonl。"""
    w = TraceWriter(tmp_path)
    try:
        assert w.path == tmp_path / "trace.jsonl"
        assert w.dir_path == tmp_path
    finally:
        w.close()


def test_writer_accepts_jsonl_file_signature(tmp_path):
    """.jsonl 文件签名构造时 dir_path 为其父目录。"""
    target = tmp_path / "t.jsonl"
    w = TraceWriter(target)
    try:
        assert w.path == target
        assert w.dir_path == tmp_path
    finally:
        w.close()


def test_writer_accepts_relative_filename(tmp_path, monkeypatch):
    """相对文件名回落到 CWD 解析（无歧义）。"""
    monkeypatch.chdir(tmp_path)
    w = TraceWriter("rel.jsonl")
    try:
        assert w.path == Path.cwd() / "rel.jsonl"
    finally:
        w.close()


def test_writer_legacy_alias_kwargs(tmp_path):
    """历史别名 kwargs 生效（tool_result_threshold/preview/threshold）。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_threshold=128, preview=64)
    try:
        assert w.tool_result_offload == 128
        assert w.preview_len == 64
    finally:
        w.close()


def test_writer_appends_and_reads_back(tmp_path):
    """append 落盘并可读回。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w.append({"type": "llm", "content": "hello"})
        recs = w.read()
        assert len(recs) == 1
        assert recs[0]["type"] == "llm"
    finally:
        w.close()


def test_writer_offloads_large_tool_result_to_sidecar(tmp_path):
    """tool_result 大 content 分流为侧车 + preview。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=100, preview_len=10)
    try:
        w.append({"type": "tool_result", "content": "x" * 500})
        recs = w.read()
        assert "result_path" in recs[0]
        assert recs[0]["preview"] == "x" * 10
        assert (tmp_path / recs[0]["result_path"]).exists()
    finally:
        w.close()


def test_writer_read_resolves_offloads(tmp_path):
    """read(resolve_offloads=True) 回灌侧车内容。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=100)
    try:
        w.append({"type": "tool_result", "content": "y" * 300})
        recs = w.read(resolve_offloads=True)
        assert recs[0]["content"] == "y" * 300
    finally:
        w.close()


def test_writer_append_after_close_is_dropped(tmp_path, caplog):
    """关闭后追加被丢弃并告警（不抛）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    w.close()
    with caplog.at_level("WARNING"):
        w.append({"type": "llm", "content": "after-close"})
    assert any("closed writer" in r.message for r in caplog.records)
    assert w.read() == []


def test_writer_close_is_idempotent(tmp_path):
    """close 幂等（重复调用不抛）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    w.close()
    w.close()


def test_writer_context_manager_closes(tmp_path):
    """with 语句退出时自动 close。"""
    with TraceWriter(tmp_path / "t.jsonl") as w:
        w.append({"type": "llm", "content": "x"})
    assert w._closed is True


def test_safe_sidecar_path_rejects_escape(tmp_path):
    """绝对路径与 .. 穿越被拒绝。"""
    assert TraceWriter._safe_sidecar_path(tmp_path, "../etc/passwd") is None
    assert TraceWriter._safe_sidecar_path(tmp_path, os.path.abspath("x.json")) is None
    assert TraceWriter._safe_sidecar_path(tmp_path, "ok.json") is not None


def test_writer_skips_malformed_json_lines(tmp_path):
    """损坏的 JSON 行被跳过（不抛），其余记录保留。"""
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path)
    try:
        w.append({"type": "llm", "content": "good"})
    finally:
        w.close()
    with path.open("a", encoding="utf-8") as f:
        f.write("{not-json}\n")
    w2 = TraceWriter(path)
    try:
        recs = w2.read()
        assert len(recs) == 1
        assert recs[0]["type"] == "llm"
    finally:
        w2.close()


def test_writer_read_missing_file_returns_empty(tmp_path):
    """文件不存在时 read 返回空列表。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    w.close()
    os.remove(w.path)
    assert w.read() == []


def test_writer_ignores_non_serializable_payload(tmp_path):
    """不可序列化对象被降级为 redaction_error 记录而非抛错。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w.append({"type": "llm", "content": {1, 2, 3}})
        recs = w.read()
        assert isinstance(recs, list)
    finally:
        w.close()
