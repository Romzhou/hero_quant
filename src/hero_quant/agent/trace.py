"""TraceWriter: 崩溃安全的 JSONL trace writer（轻量版）.

借鉴 vibe-trading agent/src/agent/trace.py 的崩溃安全与 sidecar 设计：
- 每次写入 flush + fsync
- 大记录（json dumps 长度 > threshold）落 sidecar：tmp → fsync → os.replace → dir fsync
- trace 行仅保留 {"sidecar": relpath} 引用
- _safe_sidecar_path 防目录穿越
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class TraceWriter:
    """崩溃安全的 JSONL TraceWriter.

    Args:
        trace_path: trace.jsonl 文件路径（父目录自动创建）。
        sidecar_threshold: json dumps 字符长度阈值，超过则走 sidecar。
    """

    def __init__(self, trace_path: Path | str, sidecar_threshold: int = 50) -> None:
        self.path = Path(trace_path)
        # 目录不存在时自动创建
        self.dir_path = self.path.parent if str(self.path.parent) not in ("", ".") else Path(".")
        if str(self.dir_path) != ".":
            self.dir_path.mkdir(parents=True, exist_ok=True)
        else:
            # 相对当前目录的情况
            self.dir_path = Path.cwd()
            self.path = self.dir_path / self.path.name
        self.sidecar_threshold = sidecar_threshold
        self._fsync_warned = False
        created = not self.path.exists()
        self._file = open(self.path, "a", encoding="utf-8")
        try:
            if created:
                self._fsync_dir(self.dir_path)
        except Exception:
            self._file.close()
            raise

    def append(self, obj: Dict[str, Any]) -> None:
        """追加一条记录，崩溃安全.

        - json.dumps 后若 len > threshold：先原子落 sidecar，再写 {"sidecar": relpath}
        - 否则直接写行
        - 每次写后 flush + fsync，sidecar 也保证先于引用落盘
        """
        raw = json.dumps(obj, ensure_ascii=False)
        if len(raw) > self.sidecar_threshold:
            # 计算稳定 hash 作为 sidecar 文件名
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            sidecar_name = f"{digest[:16]}.json"
            sidecar_path = self.dir_path / sidecar_name
            # 若已存在相同内容则复用，避免重复落盘
            if not sidecar_path.exists():
                self._write_sidecar_durable(sidecar_path, raw)
            # 相对路径（相对于 trace 文件所在目录）
            try:
                rel = sidecar_path.relative_to(self.dir_path)
                rel_str = rel.as_posix()
            except ValueError:
                rel_str = sidecar_name
            rec: Dict[str, Any] = {"sidecar": rel_str}
            line = json.dumps(rec, ensure_ascii=False) + "\n"
        else:
            line = raw + "\n"

        self._file.write(line)
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError as exc:
            self._warn_fsync_failure(exc, self.path)

    def close(self) -> None:
        """关闭文件句柄."""
        try:
            self._file.close()
        except Exception:
            pass

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 崩溃安全内部方法 --

    def _write_sidecar_durable(self, path: Path, value: str) -> None:
        """原子且尽量持久地写入 sidecar 文件.

        顺序: tmp 写全量 → fsync → os.replace → parent dir fsync
        rename 保证原子性：崩溃后要么没有 sidecar，要么是完整的。
        """
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
        os.replace(tmp, path)
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
        """仅当 sidecar 路径位于 base 内时返回 Path，否则 None（防穿越）."""
        root = base.resolve()
        candidate = (base / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate
