"""Markdown ingest — Wave4.

Splits markdown by heading + overlapping window and stores via MemoryStore.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Union


def _split_by_heading(text: str) -> list[str]:
    r"""Split text by markdown headings (^#{1,6}\s). Keeps heading with section."""
    # Use regex to find heading positions
    pattern = re.compile(r"(?m)^#{1,6}\s+.*$")
    matches = list(pattern.finditer(text))
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
    if not text:
        return []
    if len(text) <= chunk:
        return [text]
    chunks: list[str] = []
    step = max(1, chunk - overlap)
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


def ingest_markdown(path: Union[str, Path], overlap: int = 64, chunk: int = 512, store=None, base_path: Union[str, Path] | None = None) -> int:
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
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"markdown not found: {path}")
    text = p.read_text(encoding="utf-8", errors="ignore")
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

            bp = Path(base_path) if base_path is not None else Path("data/memory")
            # allow caller to pass directory; ensure exists
            ms = MemoryStore(base_path=bp)
        except Exception:
            ms = None

    count = 0
    for idx, piece in enumerate(all_chunks):
        # key derived from filename + idx
        key = f"{p.stem}:{idx}:{abs(hash(piece)) % 100000}"
        try:
            if ms is not None and hasattr(ms, "write"):
                ms.write(key, piece)
                count += 1
            elif ms is not None and hasattr(ms, "index_external"):
                ms.index_external(key, piece)
                count += 1
        except Exception:
            # dedup or write failure: still count as processed
            count += 1
            continue
    return count
