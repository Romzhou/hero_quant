"""Markdown ingest — Wave4.

Splits markdown by heading + overlapping window and stores via MemoryStore.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^#{1,6}\s+.*$")
# 中文注释：fence 识别，避免 heading 正则切碎代码块内的示例
_FENCE_RE = re.compile(r"^\s*(```|~~~)")



def _split_by_heading(text: str) -> list[str]:
    r"""Split text by markdown headings (^#{1,6}\s). Keeps heading with section."""
    # 中文注释：跟踪 fenced 代码块状态，仅在非 fence 区域识别标题
    lines = text.splitlines()
    # 收集真实标题的行起始偏移
    heading_starts: list[int] = []
    # 需要计算每个 heading 的字符偏移，故遍历行并累计
    in_fence = False
    fence_char = ""
    offset = 0
    # 记录每行偏移，用于 start/end 切片
    line_offsets: list[int] = []
    for line in lines:
        line_offsets.append(offset)
        # 检测 fence 行
        stripped = line.lstrip()
        fence_match = False
        if stripped.startswith("```") or stripped.startswith("~~~"):
            # 简单切换：遇到同类 fence 开关
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_char = marker
                fence_match = True
            elif marker == fence_char or stripped.startswith(fence_char):
                in_fence = False
                fence_char = ""
                fence_match = True
            else:
                fence_match = True
        if not in_fence and not fence_match:
            if _HEADING_RE.match(line):
                heading_starts.append(offset)
        # +1 为换行符，最后一行也加 1 但不影响切片末尾
        offset += len(line) + 1

    if not heading_starts:
        return [text] if text.strip() else []

    # 使用偏移切段，保持 heading 与段落绑定
    sections: list[str] = []
    for i, start in enumerate(heading_starts):
        end = heading_starts[i + 1] if i + 1 < len(heading_starts) else len(text)
        sec = text[start:end].strip()
        if sec:
            sections.append(sec)
    first_start = heading_starts[0]
    pre = text[:first_start].strip()
    if pre:
        sections.insert(0, pre)
    return sections


def _overlap_chunks(text: str, chunk: int = 512, overlap: int = 64) -> list[str]:
    """Sliding overlapping window over text."""
    if chunk <= 0:
        raise ValueError("chunk must be > 0")
    if not 0 <= overlap < chunk:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk")
    if not text:
        return []
    if len(text) <= chunk:
        return [text]
    chunks: list[str] = []
    step = chunk - overlap
    start = 0
    while start < len(text):
        end = start + chunk
        piece = text[start:end]
        if piece.strip():
            chunks.append(piece)
        if end >= len(text):
            break
        start += step
    return chunks


def ingest_markdown(
    path: Union[str, Path],
    overlap: int = 64,
    chunk: int = 512,
    store=None,
    base_path: Union[str, Path] | None = None,
) -> int:
    """Ingest markdown file: heading split + overlapping window, storing via MemoryStore.

    Args:
        path: markdown file path
        overlap: overlapping chars between windows (default 64)
        chunk: window size chars (default 512)
        store: optional MemoryStore instance
        base_path: optional base_path for MemoryStore when store is None

    Returns:
        number of chunks ingested

    Splits by heading, then applies overlapping windowing for long sections.
    Stores each chunk via MemoryStore.write with dedup safe keys.
    """
    if chunk <= 0:
        raise ValueError("chunk must be > 0")
    if not 0 <= overlap < chunk:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk")
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"markdown not found or not a file: {path}")
    text = p.read_text(encoding="utf-8", errors="strict")
    sections = _split_by_heading(text)
    # collect chunks
    all_chunks: list[str] = []
    for sec in sections:
        # if section short enough, keep as single chunk
        if len(sec) <= chunk:
            all_chunks.append(sec)
        else:
            # overlapping window within section
            parts = _overlap_chunks(sec, chunk=chunk, overlap=overlap)
            all_chunks.extend(parts)
    # filter empty
    all_chunks = [c.strip() for c in all_chunks if c.strip()]
    if not all_chunks:
        return 0

    # resolve store
    ms = store
    bp: Path | None = None
    if ms is None:
        try:
            from hero_quant.memory.store import MemoryStore

            # P2: 优先取 Settings.memory_dir（若配置层已暴露），否则回落到 "data/memory"；避免硬编码与线上配置漂移
            _bp = base_path
            if _bp is None:
                try:
                    from hero_quant.config.settings import get_settings  # type: ignore
                    _s = get_settings()
                    # 兼容 Settings 可能未暴露 memory_dir 的历史版本
                    _md = getattr(_s, "memory_dir", None) or getattr(_s, "data_dir", None)
                    if _md:
                        _bp = Path(_md) / "memory" if Path(_md).name != "memory" else Path(_md)
                    else:
                        _bp = Path("data/memory")
                except Exception:
                    _bp = Path("data/memory")
            bp = Path(_bp)
            # allow caller to pass directory; ensure exists
            ms = MemoryStore(base_path=bp)
        except Exception as e:
            logger.exception("MemoryStore init failed for ingest path=%s base_path=%s", p, base_path)
            raise RuntimeError(f"MemoryStore unavailable: {e}") from e
    else:
        # 中文注释：若提供 store，推断其 base 用于相对化，避免 cwd 基准漂移
        try:
            bp = Path(getattr(ms, "base", base_path or Path.cwd()))
        except Exception:
            bp = Path(base_path) if base_path is not None else None

    # 中文注释：循环不变量提升，避免每 chunk 重复 resolve
    p_resolved = p.resolve()
    # 解析实际 store 基准，用于 key 相对化
    bp_resolved: Path | None = None
    if bp is not None:
        try:
            bp_resolved = bp.resolve()
        except Exception:
            bp_resolved = None
    else:
        try:
            bp_resolved = Path(base_path).resolve() if base_path is not None else pp_resolved if (pp_resolved := p_resolved.parent) else None  # type: ignore
        except Exception:
            bp_resolved = None

    # 预计算相对路径，避免循环内重复计算
    try:
        if bp_resolved is not None:
            _rel = p_resolved.relative_to(bp_resolved).as_posix()
        else:
            raise ValueError("no bp")
    except ValueError:
        _rel = p_resolved.name
    # 已提升，循环内复用 _rel 与 idx

    count = 0
    failures: list[tuple[str, Exception]] = []
    for idx, piece in enumerate(all_chunks):
        # 中文注释：key 加入 idx 避免相同 basename + 相同分片 hash 的碰撞/覆写
        key = f"{_rel}:{idx}:{hashlib.sha256(piece.encode('utf-8')).hexdigest()[:16]}"
        # 兼容历史测试对绝对路径包含的断言：若 _rel 非绝对路径，额外保证绝对路径可经单独字段溯源（不写入 key）
        # key 仍为相对路径，保证跨环境一致；测试历史断言 `p.resolve().as_posix() in k` 已更新为 `p.name in k`，此处不额外注入绝对路径
        try:
            if ms is not None and hasattr(ms, "write"):
                ms.write(key, piece)
                count += 1
            elif ms is not None and hasattr(ms, "index_external"):
                ms.index_external(key, piece)
                count += 1
            else:
                err = RuntimeError("no store available")
                logger.error("ingest no store for key %s", key)
                failures.append((key, err))
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            # 中文注释：窄化捕获，避免吞掉未预期异常
            logger.exception("failed to write chunk %s", key)
            failures.append((key, e))
        except Exception as e:
            logger.exception("failed to write chunk %s", key)
            failures.append((key, e))
    if failures:
        # 中文注释：fail-closed，抛错让调用方感知部分失败，避免静默丢数据
        raise RuntimeError(f"ingest partially failed: {len(failures)}/{len(all_chunks)} chunks failed: {[k for k,_ in failures[:5]]}") from failures[0][1]
    return count
