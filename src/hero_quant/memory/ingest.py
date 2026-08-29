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

_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+.*$")


def _split_by_heading(text: str) -> list[str]:
    r"""Split text by markdown headings (^#{1,6}\s). Keeps heading with section."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [text] if text.strip() else []
    sections: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sec = text[start:end].strip()
        if sec:
            sections.append(sec)
    # Prepend leading content before first heading if any
    first_start = matches[0].start()
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

    count = 0
    failures: list[tuple[str, Exception]] = []
    for piece in all_chunks:
        # P2: key 需可移植且不泄露宿主机绝对路径；旧实现用 p.resolve().as_posix() 会写入临时目录前缀
        # 现改为 相对路径（基于 base_path 或 cwd）+ hash，相对路径失败时回落 p.name
        try:
            _bp_for_rel = Path(base_path) if base_path is not None else Path.cwd()
            _rel = p.resolve().relative_to(_bp_for_rel.resolve()).as_posix()
        except Exception:
            _rel = p.name
        key = f"{_rel}:{hashlib.sha256(piece.encode()).hexdigest()[:16]}"
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
        except Exception as e:
            logger.exception("failed to write chunk %s", key)
            failures.append((key, e))
    if failures:
        logger.warning("ingest completed with %d failures out of %d chunks", len(failures), len(all_chunks))
    return count
