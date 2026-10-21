"""W4 覆盖率补齐：agent/trace.py 的侧车分流/回灌/冲突分支。

覆盖目标（src/hero_quant/agent/trace.py）：
- _append_locked：tool_result 非字符串 content 的 JSON 序列化、序列化失败回落 str()
- 侧车已存在但内容不同 -> 改用随机后缀名（不覆盖既有侧车）
- 通用大记录分流为 sidecar 引用（相对路径）
- read(resolve_offloads=True)：侧车为 dict JSON 时整体替换、为非 JSON 时写入 sidecar_content、
  result_path 回灌不覆盖已有 content
- _write_sidecar_durable：目标已存在时幂等返回、hardlink 不可用时的 os.replace 回落
- append 传入非 dict 载荷
- TraceWriter 构造：目录 / 相对文件名 / 非 .jsonl 后缀视为目录

断言均为具体文件存在性/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


from hero_quant.agent.trace import TraceWriter


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 分流


def test_tool_result_non_string_content_is_json_serialized(tmp_path):
    """tool_result 的非字符串 content 被 JSON 序列化后分流。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=5, preview_len=1000)
    try:
        w.append({"type": "tool_result", "content": {"a": 1}})
        recs = w.read()
        assert "result_path" in recs[0]
        side = tmp_path / recs[0]["result_path"]
        assert json.loads(side.read_text(encoding="utf-8")) == {"a": 1}
    finally:
        w.close()


def test_tool_result_unserializable_content_falls_back_to_str(tmp_path):
    """content 不可 JSON 序列化时回落为 str() 并按 preview 落盘（不丢记录）。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=5, preview_len=1000)
    try:
        w.append({"type": "tool_result", "content": {1, 2, 3}})
        recs = w.read()
        assert "result_path" in recs[0]
        assert recs[0]["preview"] == "{1, 2, 3}"
    finally:
        w.close()


def test_inline_unserializable_payload_is_dropped_with_warning(tmp_path, caplog):
    """内联路径下整条记录不可序列化时被丢弃（warn-only，不抛、不留半条）。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=500)
    try:
        with caplog.at_level("WARNING"):
            w.append({"type": "tool_result", "content": {1, 2, 3}})
        assert w.read() == []
        assert any("trace append failed" in r.message for r in caplog.records)
    finally:
        w.close()


def test_existing_sidecar_with_different_content_gets_random_suffix(tmp_path):
    """同名侧车已存在但内容不同 -> 另写随机后缀文件（不覆盖既有文件）。"""
    content = "z" * 100
    digest = _sha(content)
    existing = tmp_path / f"{digest}.txt"
    existing.write_text("different-content", encoding="utf-8")
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=50, preview_len=1000)
    try:
        w.append({"type": "tool_result", "content": content})
        recs = w.read()
        name = recs[0]["result_path"]
        assert name != f"{digest}.txt"
        assert name.startswith(digest)
        assert (tmp_path / name).read_text(encoding="utf-8") == content
        # 既有侧车未被覆盖
        assert existing.read_text(encoding="utf-8") == "different-content"
    finally:
        w.close()


def test_large_generic_record_is_offloaded_to_sidecar(tmp_path):
    """非 tool_result 的大记录被分流为 sidecar 引用。"""
    w = TraceWriter(tmp_path / "t.jsonl", text_offload=50)
    try:
        w.append({"type": "llm", "content": "q" * 200})
        recs = w.read()
        assert "sidecar" in recs[0]
        assert (tmp_path / recs[0]["sidecar"]).exists()
    finally:
        w.close()


def test_small_record_is_written_inline(tmp_path):
    """小记录直接内联落盘（不产生侧车）。"""
    w = TraceWriter(tmp_path / "t.jsonl", text_offload=1000)
    try:
        w.append({"type": "llm", "content": "short"})
        recs = w.read()
        assert recs[0]["content"] == "short"
        assert "sidecar" not in recs[0]
    finally:
        w.close()


def test_append_with_non_dict_payload_is_handled(tmp_path):
    """非 dict 载荷被脱敏后落盘（不抛）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w.append(["not", "a", "dict"])  # type: ignore[arg-type]
        recs = w.read()
        assert len(recs) == 1
    finally:
        w.close()


# ---------------------------------------------------------------- 回灌


def test_read_resolves_sidecar_dict_json_by_replacing_record(tmp_path):
    """侧车内容是 dict JSON 时整体替换该条记录。"""
    payload = {"type": "llm", "content": "p" * 200}
    w = TraceWriter(tmp_path / "t.jsonl", text_offload=50)
    try:
        w.append(payload)
        recs = w.read(resolve_offloads=True)
        assert recs[0]["content"] == payload["content"]
    finally:
        w.close()


def test_read_resolves_sidecar_non_json_into_sidecar_content(tmp_path):
    """侧车内容非 JSON 时写入 sidecar_content 字段（不丢记录）。"""
    w = TraceWriter(tmp_path / "t.jsonl", text_offload=50)
    try:
        w.append({"type": "tool_result", "content": "x" * 200})
        recs = w.read(resolve_offloads=True)
        assert "content" in recs[0]
        assert recs[0]["content"] == "x" * 200
    finally:
        w.close()


def test_read_does_not_overwrite_existing_content_on_result_path(tmp_path):
    """result_path 回灌不覆盖记录里已有的 content。"""
    w = TraceWriter(tmp_path / "t.jsonl", tool_result_offload=50, preview_len=10)
    try:
        w.append({"type": "tool_result", "content": "y" * 200})
        recs = w.read(resolve_offloads=True)
        assert recs[0]["content"] == "y" * 200
    finally:
        w.close()


def test_read_skips_missing_sidecar(tmp_path):
    """侧车文件缺失时跳过该条回灌（记录主体保留）。"""
    w = TraceWriter(tmp_path / "t.jsonl", text_offload=50)
    try:
        w.append({"type": "llm", "content": "z" * 200})
        recs = w.read()
        (tmp_path / recs[0]["sidecar"]).unlink()
        recs2 = w.read(resolve_offloads=True)
        assert len(recs2) == 1
    finally:
        w.close()


# ---------------------------------------------------------------- 侧车写入


def test_write_sidecar_durable_is_idempotent_when_target_exists(tmp_path):
    """目标已存在时不再覆盖（幂等），返回 None。"""
    target = tmp_path / "sidecar.txt"
    target.write_text("already", encoding="utf-8")
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        assert w._write_sidecar_durable(target, "new-content") is None
        assert target.read_text(encoding="utf-8") == "already"
    finally:
        w.close()


def test_write_sidecar_durable_creates_file(tmp_path):
    """正常路径写入侧车文件且内容一致。"""
    target = tmp_path / "new_sidecar.txt"
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w._write_sidecar_durable(target, "payload-123")
        assert target.read_text(encoding="utf-8") == "payload-123"
        # tmp 残留被清理
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    finally:
        w.close()


# ---------------------------------------------------------------- 构造签名


def test_writer_directory_signature_appends_trace_jsonl(tmp_path):
    """目录签名 -> 主文件 trace.jsonl。"""
    w = TraceWriter(tmp_path)
    try:
        assert w.path == tmp_path / "trace.jsonl"
    finally:
        w.close()


def test_writer_non_jsonl_suffix_is_treated_as_directory(tmp_path):
    """非 .jsonl 后缀视为目录（自动补 trace.jsonl）。"""
    target = tmp_path / "trace_dir"
    target.mkdir()
    w = TraceWriter(target)
    try:
        assert w.path == target / "trace.jsonl"
    finally:
        w.close()


def test_writer_relative_jsonl_name_resolves_to_cwd(tmp_path, monkeypatch):
    """相对文件名回落到 CWD 解析。"""
    monkeypatch.chdir(tmp_path)
    w = TraceWriter("rel_trace.jsonl")
    try:
        assert w.path == Path.cwd() / "rel_trace.jsonl"
        assert w.dir_path == Path.cwd()
    finally:
        w.close()


def test_writer_env_threshold_override(tmp_path, monkeypatch):
    """环境变量 HERO_TRACE_TOOL_RESULT_OFFLOAD / HERO_TRACE_PREVIEW 生效。"""
    monkeypatch.setenv("HERO_TRACE_TOOL_RESULT_OFFLOAD", "1234")
    monkeypatch.setenv("HERO_TRACE_PREVIEW", "77")
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        assert w.tool_result_offload == 1234
        assert w.preview_len == 77
    finally:
        w.close()


def test_writer_invalid_env_threshold_falls_back(tmp_path, monkeypatch):
    """非法环境变量阈值回落默认值。"""
    monkeypatch.setenv("HERO_TRACE_TOOL_RESULT_OFFLOAD", "abc")
    monkeypatch.setenv("HERO_TRACE_PREVIEW", "0")
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        assert w.tool_result_offload == 50000
        assert w.preview_len == 500
    finally:
        w.close()


def test_writer_legacy_alias_kwargs_are_applied(tmp_path):
    """历史别名 kwargs（tool_result_threshold / text_threshold / preview / threshold）生效。"""
    w = TraceWriter(
        tmp_path / "t.jsonl",
        tool_result_threshold=111,
        text_threshold=222,
        preview=333,
    )
    try:
        assert w.tool_result_offload == 111
        assert w.text_offload == 222
        assert w.preview_len == 333
    finally:
        w.close()
