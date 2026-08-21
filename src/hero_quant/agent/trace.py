"""TraceWriter: 崩溃安全的 JSONL trace writer（加固版）.

加固版特性（Wave A2+A7）：
- 统一阈值: TOOL_RESULT_OFFLOAD=50000 TEXT_OFFLOAD=50000 PREVIEW=500 支持 HERO_TRACE_* env
- 构造函数兼容 dir_path 与 file path 两种签名；支持 sidecar_threshold / hard_threshold 别名
- 侧车持久化: tmp(pid).txt → fsync → link(tmp,final) EEXIST 不覆盖 → fsync(dir)
- read(resolve_offloads) 回灌能力 + _safe_sidecar_path allowlist
- 对 tool_result 大 content 以 result_path + preview 落盘，通用大记录以 sidecar 引用落盘
- FC 格式化（Wave A7）：模型侧截断由 config/limits TOOL_RESULT_LIMIT=10000 + tools/redaction choke 负责，trace 侧车保持 50k 分流（result_path+preview）
- 保持对旧测试（sidecar_threshold=50 + trace.jsonl 文件路径）的完全兼容

参考 vibe-trading agent/src/agent/trace.py 的崩溃安全设计。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from hero_quant.security.redaction import ARGUMENTS_SINK, RESULT_SINK, redact_payload

logger = logging.getLogger(__name__)

# 默认阈值（可被 env 或构造参数覆盖）
DEFAULT_TOOL_RESULT_OFFLOAD = 50000
DEFAULT_TEXT_OFFLOAD = 50000
DEFAULT_PREVIEW = 500


def _env_int(name: str, default: int | None) -> int | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


class TraceWriter:
    """崩溃安全的 JSONL TraceWriter（加固版）.

    Args:
        trace_path: 目录路径（新建 trace.jsonl 于目录内）或直接的 trace.jsonl 文件路径。
            为兼容历史测试，两种均支持。
        sidecar_threshold: 兼容别名，等价于 tool_result_offload / text_offload。
            若提供，则同时覆盖 TOOL_RESULT_OFFLOAD 与 TEXT_OFFLOAD（除非显式另传）。
        hard_threshold: 兼容别名，等价于 PREVIEW 长度（默认 500）。
        tool_result_offload: tool_result content 长度阈值（默认 50000，可用 HERO_TRACE_TOOL_RESULT_OFFLOAD 覆盖）
        text_offload: 通用 json dumps 长度阈值（默认 50000，可用 HERO_TRACE_TEXT_OFFLOAD 覆盖）
        preview_len: preview 截断长度（默认 500，可用 HERO_TRACE_PREVIEW 覆盖）
    """

    def __init__(
        self,
        trace_path: Path | str,
        sidecar_threshold: int | None = None,
        hard_threshold: int | None = None,
        tool_result_offload: int | None = None,
        text_offload: int | None = None,
        preview_len: int | None = None,
        **kwargs: Any,
    ) -> None:
        # 兼容 kwargs 别名（防止未来调用方使用不同命名）
        if tool_result_offload is None and "tool_result_threshold" in kwargs:
            tool_result_offload = kwargs.pop("tool_result_threshold")
        if text_offload is None and "text_threshold" in kwargs:
            text_offload = kwargs.pop("text_threshold")
        if preview_len is None and "preview" in kwargs:
            preview_len = kwargs.pop("preview")
        if hard_threshold is None and "preview_size" in kwargs:
            hard_threshold = kwargs.pop("preview_size")
        # sidecar_threshold 也可能以其他 env 别名传入
        if sidecar_threshold is None and "threshold" in kwargs:
            sidecar_threshold = kwargs.pop("threshold")

        # --- 路径解析：兼容目录与文件两种签名 ---
        p = Path(trace_path)
        # 优先以 is_dir 判断；若不存在则以后缀启发
        if p.is_dir():
            self.dir_path = p
            self.path = self.dir_path / "trace.jsonl"
        elif p.suffix.lower() == ".jsonl":
            self.path = p
            # 与旧实现保持一致：parent 为 "" 或 "." 时落在 cwd
            parent = self.path.parent
            if str(parent) in ("", "."):
                self.dir_path = Path.cwd()
                self.path = self.dir_path / self.path.name
            else:
                self.dir_path = parent
        else:
            # 字符串传入且不存在，启发：含 .jsonl 视为文件，否则视为目录（新 API）
            s = str(trace_path)
            if s.endswith(".jsonl"):
                self.path = p
                parent = p.parent
                if str(parent) in ("", "."):
                    self.dir_path = Path.cwd()
                    self.path = self.dir_path / self.path.name
                else:
                    self.dir_path = parent
            else:
                # 视为目录（Wave A2 新签名 TraceWriter(tmp_path)）
                self.dir_path = p
                self.path = self.dir_path / "trace.jsonl"

        # 目录确保存在
        if str(self.dir_path) != ".":
            self.dir_path.mkdir(parents=True, exist_ok=True)
        else:
            self.dir_path = Path.cwd()
            self.path = self.dir_path / self.path.name
            self.dir_path.mkdir(parents=True, exist_ok=True)

        # --- 阈值解析：env > 构造参数 > 默认 ---
        # env 优先级：HERO_TRACE_* 仅在参数未显式提供时生效
        env_tool = _env_int("HERO_TRACE_TOOL_RESULT_OFFLOAD", None)
        if env_tool is None:
            env_tool = _env_int("HERO_TRACE_SIDECAR_THRESHOLD", None)
        env_text = _env_int("HERO_TRACE_TEXT_OFFLOAD", None)
        env_preview = _env_int("HERO_TRACE_PREVIEW", None)
        if env_preview is None:
            env_preview = _env_int("HERO_TRACE_HARD_THRESHOLD", None)

        # tool_result_offload
        if sidecar_threshold is not None:
            # sidecar_threshold 作为通用覆盖（兼容旧阈值 50）
            self.tool_result_offload = int(sidecar_threshold)
            self.text_offload = int(sidecar_threshold)  # 旧语义下两者一致
        else:
            if tool_result_offload is not None:
                self.tool_result_offload = int(tool_result_offload)
            elif env_tool is not None:
                self.tool_result_offload = int(env_tool)
            else:
                self.tool_result_offload = DEFAULT_TOOL_RESULT_OFFLOAD

            if text_offload is not None:
                self.text_offload = int(text_offload)
            elif env_text is not None:
                self.text_offload = int(env_text)
            else:
                # 若 env_text 未设且 sidecar 未设，则独立默认为 50000；若 sidecar 已设则已在上分支处理
                self.text_offload = DEFAULT_TEXT_OFFLOAD

        # 显式 tool/text 参数覆盖 sidecar 带来的默认值（若同时传入）
        if tool_result_offload is not None:
            self.tool_result_offload = int(tool_result_offload)
        if text_offload is not None:
            self.text_offload = int(text_offload)

        # preview
        if hard_threshold is not None:
            self.preview_len = int(hard_threshold)
        elif preview_len is not None:
            self.preview_len = int(preview_len)
        elif env_preview is not None:
            self.preview_len = int(env_preview)
        else:
            self.preview_len = DEFAULT_PREVIEW

        # 保留旧属性名以兼容读取
        self.sidecar_threshold = self.tool_result_offload
        self.hard_threshold = self.preview_len

        self._fsync_warned = False
        self._lock = threading.RLock()
        created = not self.path.exists()
        self._file = open(self.path, "a", encoding="utf-8")
        try:
            if created:
                self._fsync_dir(self.dir_path)
        except Exception:
            self._file.close()
            raise

    # -- 公共 API --

    def append(self, obj: Dict[str, Any]) -> None:
        """追加一条记录，崩溃安全（线程安全：原子追加）.

        分支：
        - type == tool_result 且 content 长度 > tool_result_offload → 落 result_path + preview
        - 否则若 json dumps 长度 > text_offload → 落 sidecar 引用
        - 否则直接 inline
        """
        # B2-1 auto redaction waterfall: RESULT_SINK allows content passthrough, otherwise strict
        try:
            if isinstance(obj, dict):
                sink = RESULT_SINK if obj.get("type") == "tool_result" else ARGUMENTS_SINK
                obj = redact_payload(obj, sink=sink)
        except Exception:
            pass
        with self._lock:
            # 优先处理 tool_result 大 content（Wave A2 新阈值）
            if isinstance(obj, dict) and obj.get("type") == "tool_result" and "content" in obj:
                content = obj["content"]
                # 统一转为字符串度量长度（测试用 str）
                if isinstance(content, str):
                    content_str = content
                else:
                    # 非字符串 content 以 json 长度度量
                    try:
                        content_str = json.dumps(content, ensure_ascii=False)
                    except Exception:
                        content_str = str(content)
                if len(content_str) > self.tool_result_offload:
                    digest = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
                    sidecar_name = f"{digest[:16]}.txt"
                    sidecar_path = self.dir_path / sidecar_name
                    if not sidecar_path.exists():
                        self._write_sidecar_durable(sidecar_path, content_str)
                    preview = content_str[: self.preview_len]
                    # 保留除 content 外的所有字段，并注入 result_path/preview
                    rec: Dict[str, Any] = {k: v for k, v in obj.items() if k != "content"}
                    rec["result_path"] = sidecar_name
                    rec["preview"] = preview
                    line = json.dumps(rec, ensure_ascii=False) + "\n"
                    self._append_line_locked(line)
                    return

            # 通用大记录侧车（兼容旧逻辑，阈值使用 text_offload）
            raw = json.dumps(obj, ensure_ascii=False)
            if len(raw) > self.text_offload:
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                sidecar_name = f"{digest[:16]}.json"
                sidecar_path = self.dir_path / sidecar_name
                if not sidecar_path.exists():
                    self._write_sidecar_durable(sidecar_path, raw)
                try:
                    rel = sidecar_path.relative_to(self.dir_path)
                    rel_str = rel.as_posix()
                except ValueError:
                    rel_str = sidecar_name
                rec = {"sidecar": rel_str}
                line = json.dumps(rec, ensure_ascii=False) + "\n"
            else:
                line = raw + "\n"

            self._append_line_locked(line)

    def _append_line(self, line: str) -> None:
        # Public wrapper holds lock for single-thread callers
        with self._lock:
            self._append_line_locked(line)

    def _append_line_locked(self, line: str) -> None:
        self._file.write(line)
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError as exc:
            self._warn_fsync_failure(exc, self.path)

    def close(self) -> None:
        """关闭文件句柄（线程安全）."""
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def read(self, resolve_offloads: bool = False) -> List[Dict[str, Any]]:
        """读取 trace.jsonl，必要时回灌侧车内容.

        Args:
            resolve_offloads: 若为 True，则将 sidecar / result_path 指向的文件内容回灌。
                - sidecar: 文件内为完整 json dumps，尝试解析后返回原始对象
                - result_path: 文件内为 tool_result content 字符串，回灌为 content 字段
                路径均经 _safe_sidecar_path 校验，防穿越。
        """
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except Exception:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if resolve_offloads:
                if "sidecar" in rec:
                    sp = self._safe_sidecar_path(self.dir_path, rec["sidecar"])
                    if sp is not None and sp.exists():
                        try:
                            raw = sp.read_text(encoding="utf-8")
                            try:
                                original = json.loads(raw)
                                # 若侧车内是原始对象的 json，则直接替换
                                if isinstance(original, dict):
                                    rec = original
                                else:
                                    rec["sidecar_content"] = raw
                            except Exception:
                                rec["sidecar_content"] = raw
                        except Exception:
                            pass
                elif "result_path" in rec:
                    sp = self._safe_sidecar_path(self.dir_path, rec["result_path"])
                    if sp is not None and sp.exists():
                        try:
                            content = sp.read_text(encoding="utf-8")
                            # 仅当 rec 未含 content 时回灌，避免覆盖已有的 preview 逻辑
                            if "content" not in rec:
                                rec["content"] = content
                        except Exception:
                            pass
            records.append(rec)
        return records

    # -- 崩溃安全内部方法 --

    def _write_sidecar_durable(self, path: Path, value: str) -> None:
        """原子且尽量持久地写入 sidecar 文件（线程安全）.

        顺序: tmp 写全量 → fsync → link(tmp, final) EEXIST 不覆盖 → fsync(dir)
        HardLink 发布保证不覆盖已存在快照；崩溃后要么没有侧车，要么是完整的。
        """
        with self._lock:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                payload = value.encode("utf-8")
                written = 0
                while written < len(payload):
                    written += os.write(fd, payload[written:])
                try:
                    os.fsync(fd)
                except OSError as exc:
                    self._warn_fsync_failure(exc, tmp)
            except BaseException:
                os.close(fd)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            else:
                os.close(fd)
            # HardLink 发布：若目标已存在则不覆盖
            try:
                os.link(tmp, path)
                linked = True
            except FileExistsError:
                # 已存在，不覆盖，直接清理 tmp
                linked = False
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            except OSError as exc:
                # 跨设备或 Windows 特殊情况：回退到 replace（仅当 link 不可用）
                # 但仍需保证不覆盖语义？若文件已存在则不再 replace
                if path.exists():
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    return
                try:
                    os.replace(tmp, path)
                    linked = True
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    # 记录警告但不抛出，避免 trace 写入整体失败
                    self._warn_fsync_failure(exc, path)
                    return
            # 仅在成功创建新文件后 fsync 目录并清理 tmp
            if linked:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                self._fsync_dir(path.parent)

    def _fsync_dir(self, directory: Path) -> None:
        """fsync 目录条目，使新建/重命名落盘."""
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            self._warn_fsync_failure(exc, directory)
        finally:
            os.close(dir_fd)

    def _warn_fsync_failure(self, exc: OSError, target: Path) -> None:
        if self._fsync_warned:
            return
        self._fsync_warned = True
        logger.warning(
            "trace fsync failed on %s (%s); trace durability degraded to flush-only",
            target,
            exc,
        )

    @staticmethod
    def _safe_sidecar_path(base: Path, rel_path: str) -> Path | None:
        """仅当侧车路径位于 base 内时返回 Path，否则 None（防穿越）."""
        root = base.resolve()
        candidate = (base / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate
