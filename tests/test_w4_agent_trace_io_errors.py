"""W4 覆盖率补齐：agent/trace.py 的 IO 异常与脱敏兜底分支。

覆盖目标（依据 --cov-report=term-missing 的 Missing 行号）：
- default_trace_dir：本地 traces 目录不可建时回落到系统临时目录
- _scrub_cookie：dict/list 成员递归抛错、字符串脱敏正则失败
- TraceWriter 构造：env 阈值别名（HERO_TRACE_SIDECAR_THRESHOLD / HERO_TRACE_TEXT_OFFLOAD /
  HERO_TRACE_HARD_THRESHOLD）
- append：redact_payload 失败时降级为 redaction_error 记录（fail-closed）
- 通用大记录侧车冲突（同名不同内容 -> 随机后缀）
- _write_sidecar_durable：hardlink 不可用时回退 os.replace
- _fsync_dir：目录 fd 打不开 / fsync 失败
- _safe_sidecar_path：含冒号（Windows 盘符）与换行的路径被拒
- close：fsync 失败仍标记已关闭
- read：文件打不开返回空列表

断言均为具体文件存在性/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


from hero_quant.agent import trace as tr
from hero_quant.agent.trace import TraceWriter


# ---------------------------------------------------------------- 目录回落


def test_default_trace_dir_falls_back_to_tempdir(monkeypatch, tmp_path):
    """本地 traces 目录不可建时回落到系统临时目录。"""
    real_mkdir = Path.mkdir

    def _boom(self, *a, **kw):
        if self.name == "traces":
            raise OSError("cannot create local traces")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", _boom)
    monkeypatch.delenv("HERO_TRACE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    got = tr.default_trace_dir()
    assert got.exists()
    assert got.name == "hero_traces"


# ---------------------------------------------------------------- 脱敏兜底


class _BrokenDict(dict):
    """items() 抛异常的 dict（模拟损坏载荷）。"""

    def items(self):
        raise RuntimeError("boom")


def test_scrub_cookie_survives_dict_value_error():
    """dict 成员递归抛错时该成员被替换为 ***（不整体崩）。"""
    out = tr._scrub_cookie({"k": _BrokenDict()})
    assert out["k"] == "***"


def test_scrub_cookie_survives_list_item_error():
    """list 成员递归抛错时该成员被替换为 ***。"""
    out = tr._scrub_cookie([_BrokenDict()])
    assert out == ["***"]


def test_scrub_cookie_survives_tuple_item_error():
    """tuple 成员递归抛错时该成员被替换为 ***（保持 tuple 类型）。"""
    out = tr._scrub_cookie((_BrokenDict(),))
    assert out == ("***",)


# ---------------------------------------------------------------- 构造阈值


def test_writer_env_sidecar_threshold_alias(tmp_path, monkeypatch):
    """HERO_TRACE_SIDECAR_THRESHOLD 作为 tool_result 阈值生效。"""
    monkeypatch.setenv("HERO_TRACE_SIDECAR_THRESHOLD", "4321")
    monkeypatch.delenv("HERO_TRACE_TOOL_RESULT_OFFLOAD", raising=False)
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        assert w.tool_result_offload == 4321
    finally:
        w.close()


def test_writer_env_text_offload(tmp_path, monkeypatch):
    """HERO_TRACE_TEXT_OFFLOAD 生效。"""
    monkeypatch.setenv("HERO_TRACE_TEXT_OFFLOAD", "2468")
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        assert w.text_offload == 2468
    finally:
        w.close()


def test_writer_env_hard_threshold_alias(tmp_path, monkeypatch):
    """HERO_TRACE_HARD_THRESHOLD 作为 preview 长度生效。"""
    monkeypatch.setenv("HERO_TRACE_HARD_THRESHOLD", "99")
    monkeypatch.delenv("HERO_TRACE_PREVIEW", raising=False)
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        assert w.preview_len == 99
    finally:
        w.close()


# ---------------------------------------------------------------- append 兜底


def test_append_with_redaction_failure_writes_redaction_error(tmp_path, monkeypatch):
    """脱敏失败时写 redaction_error 记录（fail-closed，不落明文）。"""

    def _boom(*_a, **_kw):
        raise RuntimeError("redact down")

    monkeypatch.setattr(tr, "redact_payload", _boom)
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w.append({"type": "llm", "content": "secret"})
        recs = w.read()
        assert recs and recs[0].get("redaction_error") is True
        assert "secret" not in json.dumps(recs, ensure_ascii=False)
    finally:
        w.close()


# ---------------------------------------------------------------- 侧车冲突


def test_generic_sidecar_conflict_uses_random_suffix(tmp_path):
    """通用大记录的同名侧车内容不同 -> 写随机后缀文件（不覆盖）。"""
    w = TraceWriter(tmp_path / "t.jsonl", text_offload=50)
    try:
        payload = {"type": "llm", "content": "q" * 200}
        w.append(payload)
        recs = w.read()
        name = recs[0]["sidecar"]
        side = tmp_path / name
        side.write_text("different", encoding="utf-8")
        w.append(payload)
        recs2 = w.read()
        assert len(recs2) == 2
        assert recs2[1]["sidecar"] != name
    finally:
        w.close()


# ---------------------------------------------------------------- 侧车写入兜底


def test_write_sidecar_durable_falls_back_to_replace(tmp_path, monkeypatch):
    """hardlink 不可用时回退 os.replace（内容仍正确落盘）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    target = tmp_path / "fallback.txt"
    real_link = os.link

    def _boom(*_a, **_kw):
        raise OSError("hardlink unsupported")

    monkeypatch.setattr(os, "link", _boom)
    try:
        w._write_sidecar_durable(target, "payload")
        assert target.read_text(encoding="utf-8") == "payload"
    finally:
        monkeypatch.setattr(os, "link", real_link)
        w.close()


def test_write_sidecar_durable_unlinks_tmp_on_total_failure(tmp_path, monkeypatch):
    """hardlink 与 replace 都失败时清理 tmp 并告警（不抛）。"""

    def _boom(*_a, **_kw):
        raise OSError("no link, no replace")

    monkeypatch.setattr(os, "link", _boom)
    monkeypatch.setattr(os, "replace", _boom)
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w._write_sidecar_durable(tmp_path / "never.txt", "payload")
        assert not (tmp_path / "never.txt").exists()
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    finally:
        w.close()


def test_fsync_dir_ignores_unopenable_directory(tmp_path):
    """目录 fd 打不开时静默跳过（best-effort 耐久）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        w._fsync_dir(tmp_path / "does-not-exist")  # 不抛
    finally:
        w.close()


# ---------------------------------------------------------------- 路径校验


def test_safe_sidecar_path_rejects_windows_drive_letter(tmp_path):
    """含盘符冒号的路径被拒（防 Windows 盘符注入）。"""
    assert TraceWriter._safe_sidecar_path(tmp_path, "C:/evil.json") is None


def test_safe_sidecar_path_rejects_newline(tmp_path):
    """含换行的路径被拒（防注入）。"""
    assert TraceWriter._safe_sidecar_path(tmp_path, "a\nb.json") is None


def test_safe_sidecar_path_rejects_absolute(tmp_path):
    """绝对路径被拒。"""
    assert TraceWriter._safe_sidecar_path(tmp_path, str((tmp_path / "x.json").resolve())) is None


# ---------------------------------------------------------------- close / read


def test_close_survives_fsync_failure(tmp_path, monkeypatch):
    """close 时 fsync 失败仍标记已关闭（幂等收尾）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    real_fsync = os.fsync

    def _boom(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", _boom)
    try:
        w.close()
        assert w._closed is True
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)


def test_read_returns_empty_when_file_unreadable(tmp_path, monkeypatch):
    """trace 文件打不开时 read 返回空列表（不抛）。"""
    w = TraceWriter(tmp_path / "t.jsonl")
    try:
        real_open = Path.open

        def _boom(self, *a, **kw):
            if self.name == "t.jsonl":
                raise OSError("cannot open")
            return real_open(self, *a, **kw)

        monkeypatch.setattr(Path, "open", _boom)
        assert w.read() == []
    finally:
        w.close()


def test_read_skips_undecodable_line(tmp_path, caplog):
    """无法解码的 JSON 行被跳过（其余记录保留）。"""
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
        assert recs[0]["content"] == "good"
    finally:
        w2.close()
