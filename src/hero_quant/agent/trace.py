"""崩溃安全的 JSONL 轨迹写入器（侧车持久化）。

职责：为 Agent 循环提供可重放、可审计的执行轨迹，与主流程解耦落盘。
架构位置：agent 层侧车组件，被 Loop/工具调用，独立于主状态持久化。
关键设计：
- 阈值分流：tool_result 按 content 长度、通用记录按 JSON 长度分流到侧车文件，主 trace 仅留 preview/sidecar 引用
- 崩溃安全：tmp(pid).tmp → fsync → hardlink 原子发布（EEXIST 不覆盖）→ fsync 目录
- 兼容与可配置：支持目录/文件两种构造签名，阈值与 preview 可由 HERO_TRACE_* 环境变量覆盖
- 安全与并发：RLock 保证线程安全，写入前按 sink 脱敏，读取时校验路径防穿越并可回灌侧车
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from hero_quant.security.redaction import ARGUMENTS_SINK, RESULT_SINK, redact_payload

logger = logging.getLogger(__name__)

# 默认阈值：可被环境变量或构造参数覆盖
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


def _validate_threshold(value: int | None, name: str, default: int) -> int:
    """Validate threshold >0 else fallback to default with warning."""
    if value is None:
        return default
    try:
        iv = int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        logger.warning("invalid %s %r, using default %d: %s", name, value, default, exc)
        return default
    if iv <= 0:
        logger.warning("%s must be >0, got %d; using default %d", name, iv, default)
        return default
    return iv


class TraceWriter:
    """崩溃安全的 JSONL 轨迹写入器，支持阈值分流与原子侧车落盘。

    不变量：并发追加线程安全；侧车文件一经创建不再覆盖；fsync 失败仅告警不阻断写入。
    关键状态：dir_path/path 指向落盘目录与主 trace 文件；三类阈值控制分流与 preview 长度。
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
        # 兼容历史别名，避免调用方命名不一致导致失效
        if tool_result_offload is None and "tool_result_threshold" in kwargs:
            tool_result_offload = kwargs.pop("tool_result_threshold")
        if text_offload is None and "text_threshold" in kwargs:
            text_offload = kwargs.pop("text_threshold")
        if preview_len is None and "preview" in kwargs:
            preview_len = kwargs.pop("preview")
        if hard_threshold is None and "preview_size" in kwargs:
            hard_threshold = kwargs.pop("preview_size")
        if sidecar_threshold is None and "threshold" in kwargs:
            sidecar_threshold = kwargs.pop("threshold")

        # 路径解析：兼容“目录”与“文件”两种签名
        p = Path(trace_path)
        if p.is_dir():
            self.dir_path = p
            self.path = self.dir_path / "trace.jsonl"
        elif p.suffix.lower() == ".jsonl":
            self.path = p
            # parent 为空或 "." 时回落到 cwd，避免相对路径歧义
            parent = self.path.parent
            if str(parent) in ("", "."):
                self.dir_path = Path.cwd()
                self.path = self.dir_path / self.path.name
            else:
                self.dir_path = parent
        else:
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
                # 视为目录简写，自动补 trace.jsonl
                self.dir_path = p
                self.path = self.dir_path / "trace.jsonl"

        if str(self.dir_path) != ".":
            self.dir_path.mkdir(parents=True, exist_ok=True)
        else:
            self.dir_path = Path.cwd()
            self.path = self.dir_path / self.path.name
            self.dir_path.mkdir(parents=True, exist_ok=True)

        # 阈值解析优先级：显式 > 通用别名 > 环境变量 > 默认值（单链，无二次覆盖）
        env_tool = _env_int("HERO_TRACE_TOOL_RESULT_OFFLOAD", None)
        if env_tool is None:
            env_tool = _env_int("HERO_TRACE_SIDECAR_THRESHOLD", None)
        env_text = _env_int("HERO_TRACE_TEXT_OFFLOAD", None)
        env_preview = _env_int("HERO_TRACE_PREVIEW", None)
        if env_preview is None:
            env_preview = _env_int("HERO_TRACE_HARD_THRESHOLD", None)

        if tool_result_offload is not None:
            self.tool_result_offload = _validate_threshold(
                tool_result_offload, "tool_result_offload", DEFAULT_TOOL_RESULT_OFFLOAD
            )
        elif sidecar_threshold is not None:
            self.tool_result_offload = _validate_threshold(
                sidecar_threshold, "sidecar_threshold", DEFAULT_TOOL_RESULT_OFFLOAD
            )
        elif env_tool is not None:
            self.tool_result_offload = _validate_threshold(
                env_tool, "HERO_TRACE_TOOL_RESULT_OFFLOAD", DEFAULT_TOOL_RESULT_OFFLOAD
            )
        else:
            self.tool_result_offload = DEFAULT_TOOL_RESULT_OFFLOAD

        if text_offload is not None:
            self.text_offload = _validate_threshold(text_offload, "text_offload", DEFAULT_TEXT_OFFLOAD)
        elif sidecar_threshold is not None:
            self.text_offload = _validate_threshold(
                sidecar_threshold, "sidecar_threshold", DEFAULT_TEXT_OFFLOAD
            )
        elif env_text is not None:
            self.text_offload = _validate_threshold(
                env_text, "HERO_TRACE_TEXT_OFFLOAD", DEFAULT_TEXT_OFFLOAD
            )
        else:
            self.text_offload = DEFAULT_TEXT_OFFLOAD

        if hard_threshold is not None:
            self.preview_len = _validate_threshold(hard_threshold, "hard_threshold", DEFAULT_PREVIEW)
        elif preview_len is not None:
            self.preview_len = _validate_threshold(preview_len, "preview_len", DEFAULT_PREVIEW)
        elif env_preview is not None:
            self.preview_len = _validate_threshold(env_preview, "HERO_TRACE_PREVIEW", DEFAULT_PREVIEW)
        else:
            self.preview_len = DEFAULT_PREVIEW

        # 保留旧属性名以兼容外部读取
        self.sidecar_threshold = self.tool_result_offload
        self.hard_threshold = self.preview_len

        self._fsync_warned = False
        self._last_fsync_warning: float = 0.0
        self._closed = False
        self._lock = threading.RLock()
        created = not self.path.exists()
        self._file = open(self.path, "a", encoding="utf-8")
        try:
            if created:
                self._fsync_dir(self.dir_path)
        except Exception:
            self._file.close()
            raise

    def append(self, obj: Dict[str, Any]) -> None:
        """追加一条记录，线程安全且尽量保证落盘持久化."""
        # 按 sink 分流脱敏：tool_result 允许 content 透传，其余严格脱敏
        try:
            if isinstance(obj, dict):
                sink = RESULT_SINK if obj.get("type") == "tool_result" else ARGUMENTS_SINK
                obj = redact_payload(obj, sink=sink)
        except Exception as exc:
            logger.warning("redact_payload failed (%s), dropping sensitive fields", exc)
            # fail-closed: do not persist unredacted obj
            obj = {"type": obj.get("type", "unknown") if isinstance(obj, dict) else "unknown", "redaction_error": True}
        with self._lock:
            if getattr(self, "_closed", False):
                raise ValueError("TraceWriter closed")
            # 分支1：tool_result 大 content 分流为 result_path + preview
            if isinstance(obj, dict) and obj.get("type") == "tool_result" and "content" in obj:
                content = obj["content"]
                if isinstance(content, str):
                    content_str = content
                else:
                    try:
                        content_str = json.dumps(content, ensure_ascii=False)
                    except Exception:
                        content_str = str(content)
                if len(content_str) > self.tool_result_offload:
                    digest = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
                    sidecar_name = f"{digest}.txt"
                    sidecar_path = self.dir_path / sidecar_name
                    if not sidecar_path.exists():
                        self._write_sidecar_durable(sidecar_path, content_str)
                    else:
                        try:
                            existing = sidecar_path.read_bytes()
                            if existing != content_str.encode("utf-8"):
                                sidecar_name = f"{digest}_{os.urandom(4).hex()}.txt"
                                sidecar_path = self.dir_path / sidecar_name
                                self._write_sidecar_durable(sidecar_path, content_str)
                        except Exception:
                            # read验证失败时用随机后缀避免覆盖
                            sidecar_name = f"{digest}_{os.urandom(4).hex()}.txt"
                            sidecar_path = self.dir_path / sidecar_name
                            self._write_sidecar_durable(sidecar_path, content_str)
                    preview = content_str[: self.preview_len]
                    rec: Dict[str, Any] = {k: v for k, v in obj.items() if k != "content"}
                    rec["result_path"] = sidecar_name
                    rec["preview"] = preview
                    line = json.dumps(rec, ensure_ascii=False) + "\n"
                    self._append_line_locked(line)
                    return

            # 分支2：通用大记录分流为 sidecar 引用；否则 inline
            raw = json.dumps(obj, ensure_ascii=False)
            if len(raw) > self.text_offload:
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                sidecar_name = f"{digest}.json"
                sidecar_path = self.dir_path / sidecar_name
                if not sidecar_path.exists():
                    self._write_sidecar_durable(sidecar_path, raw)
                else:
                    try:
                        existing = sidecar_path.read_bytes()
                        if existing != raw.encode("utf-8"):
                            sidecar_name = f"{digest}_{os.urandom(4).hex()}.json"
                            sidecar_path = self.dir_path / sidecar_name
                            self._write_sidecar_durable(sidecar_path, raw)
                    except Exception:
                        sidecar_name = f"{digest}_{os.urandom(4).hex()}.json"
                        sidecar_path = self.dir_path / sidecar_name
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
        # 对外单条写入入口，内部已加锁
        with self._lock:
            self._append_line_locked(line)

    def _append_line_locked(self, line: str) -> None:
        self._file.write(line)
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError as exc:
            self._warn_fsync_failure(exc, self.path)
            return
        self._fsync_dir(self.dir_path)

    def close(self) -> None:
        """关闭文件句柄，线程安全，幂等，关前 fsync."""
        with self._lock:
            if getattr(self, "_closed", False):
                logger.debug("TraceWriter.close called on already closed writer %s", self.path)
                return
            try:
                try:
                    os.fsync(self._file.fileno())
                except Exception as exc:
                    logger.warning("TraceWriter fsync before close failed for %s: %s", self.path, exc, exc_info=True)
                self._file.close()
            except Exception as exc:
                logger.warning("TraceWriter close failed for %s: %s", self.path, exc, exc_info=True)
            finally:
                self._closed = True

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup: ensure file descriptor not leaked if caller forgets close()
        try:
            if not getattr(self, "_closed", False):
                logger.warning("TraceWriter leaked without close() for %s, auto-closing", getattr(self, "path", "unknown"))
                self.close()
        except Exception:
            pass

    def read(self, resolve_offloads: bool = False) -> List[Dict[str, Any]]:
        """读取 trace.jsonl，resolve_offloads 为 True 时回灌侧车内容并校验路径."""
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            f = self.path.open("r", encoding="utf-8")
        except Exception as exc:
            logger.warning("trace read failed on %s: %s", self.path, exc)
            return []
        with f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception as exc:
                    logger.warning("trace json decode failed at line %d: %s", lineno, exc)
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

    def _write_sidecar_durable(self, path: Path, value: str) -> None:
        """原子持久写入侧车：tmp 全量写入 → fsync → hardlink 发布 → fsync 目录."""
        with self._lock:
            tmp = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.{os.urandom(4).hex()}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(tmp, flags, 0o600)
            try:
                payload = value.encode("utf-8")
                written = 0
                while written < len(payload):
                    written += os.write(fd, payload[written:])
                try:
                    os.fsync(fd)
                except OSError as exc:
                    self._warn_fsync_failure(exc, tmp)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            else:
                os.close(fd)
            try:
                os.link(tmp, path)
                linked = True
            except FileExistsError:
                linked = False
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            except OSError as exc:
                # 跨设备/Windows 无法 hardlink 时回退；已存在则不覆盖以保幂等
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
                    self._warn_fsync_failure(exc, path)
                    return
            if linked:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                self._fsync_dir(path.parent)

    def _fsync_dir(self, directory: Path) -> None:
        """fsync 目录，使新建/重命名条目落盘."""
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            dir_fd = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            self._warn_fsync_failure(exc, directory)
        finally:
            os.close(dir_fd)

    def _warn_fsync_failure(self, exc: OSError, target: Path) -> None:
        now = time.monotonic()
        last = getattr(self, "_last_fsync_warning", 0)
        # 限流但不永久静默：5秒内重复仅 debug，超时后再次 warning
        if self._fsync_warned and (now - last) < 5:
            logger.debug("trace fsync throttled on %s: %s", target, exc)
            return
        logger.warning(
            "trace fsync failed on %s (%s); trace durability degraded to flush-only",
            target,
            exc,
        )
        self._fsync_warned = True
        self._last_fsync_warning = now

    @staticmethod
    def _safe_sidecar_path(base: Path, rel_path: str) -> Path | None:
        """校验侧车路径在 base 内，防目录穿越；越界返回 None."""
        if os.path.isabs(rel_path) or ".." in Path(rel_path).parts:
            return None
        if "\n" in rel_path or "\x00" in rel_path or ":" in rel_path:
            # 拒绝含冒号/换行等非法字符，防止 Windows 盘符或注入
            # 但允许正常文件名中的下划线等
            if rel_path.count(":") > 0 and os.name == "nt":
                return None
            if "\n" in rel_path or "\x00" in rel_path:
                return None
        root = base.resolve()
        candidate = (base / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate
