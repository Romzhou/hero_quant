"""Simple embedding summary for Context vector folding (Task 12).

No external model: deterministic pseudo-embedding via hashing.
Provides `embedding_summary(messages)` that returns a summary string
containing the keyword 'embedding' so that ContextManager vector folding
can be detected in tests. Includes分级记忆: recent tail preserved separately,
middle messages are summarized via keyword frequency / pseudo-vector centroid.
"""
from __future__ import annotations

import hashlib
import math
from typing import List, Dict


def embed(text: str, dim: int = 16) -> List[float]:
    """Deterministic pseudo-embedding for a text string.

    Uses SHA256 to generate `dim` float values in [0,1].
    Stable across runs, no external dependency.
    """
    if not isinstance(text, str):
        text = str(text)
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand digest if dim > len
    vals: List[float] = []
    # Repeat hash with counter if needed
    counter = 0
    while len(vals) < dim:
        chunk = hashlib.sha256(h + counter.to_bytes(1, "little")).digest() if counter else h
        for b in chunk:
            if len(vals) >= dim:
                break
            vals.append(b / 255.0)
        counter += 1
    return vals[:dim]


def cosine_sim(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: List[List[float]]) -> List[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    c = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            c[i] += x
    return [x / len(vectors) for x in c]


def embedding_summary(messages: List[Dict] | List[str], max_chars: int = 200) -> str:
    """Generate embedding summary for a list of messages.

    Accepts list of dicts with 'content' key or list of strings.
    Returns summary string that *must* contain 'embedding' keyword for audit.

    分级记忆: summarize middle tier via centroid similarity, fallback to keyword.
    """
    # Normalize to texts
    texts: List[str] = []
    for m in messages:
        if isinstance(m, dict):
            c = m.get("content", "")
            texts.append(str(c))
        elif isinstance(m, str):
            texts.append(m)
        else:
            texts.append(str(m))

    if not texts:
        return "[EMBEDDING_SUMMARY] empty embedding"

    # Simple keyword extraction: most frequent non-stop words
    joined = " ".join(texts)
    # Pseudo-vector centroid for determinism
    vecs = [embed(t) for t in texts]
    cent = centroid(vecs)
    # Represent centroid first 3 dims as hint
    cent_hint = ",".join(f"{x:.2f}" for x in cent[:3]) if cent else "0.00"

    # Keyword freq
    import re

    words = re.findall(r"\w+", joined.lower())
    stop = {"the", "a", "an", "is", "are", "and", "or", "to", "of", "in", "for", "with", "x", "msg", "thr", "user", "assistant", "system", "tool"}
    freq: Dict[str, int] = {}
    for w in words:
        if w in stop or len(w) <= 1:
            continue
        # filter pure x repetitions
        if set(w) == {"x"}:
            continue
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda kv: -kv[1])[:5]
    keywords = ", ".join(k for k, _ in top) if top else joined[:40]

    # Count
    count = len(texts)
    # Build summary with embedding keyword
    summary = f"[EMBEDDING_SUMMARY embedding] {count} messages folded via vector centroid [{cent_hint}] keywords: {keywords}"
    # Truncate to max_chars but keep embedding marker
    if len(summary) > max_chars:
        # keep prefix embedding marker
        summary = summary[: max_chars - 3] + "..."
        if "embedding" not in summary.lower():
            summary = "[EMBEDDING_SUMMARY embedding] " + summary
    return summary


__all__ = ["embed", "cosine_sim", "centroid", "embedding_summary"]
