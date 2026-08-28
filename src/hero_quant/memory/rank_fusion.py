"""Uniform RRF + cosine fusion (0.5/0.5).

Provides rank_fusion(bm25_cands, vec_cands, k=60) -> List[Tuple[str, float]]
where inputs are iterables of (key, score). RRF derived from rank order
(sorted desc by score), cosine normalized by max, uniform 0.5 weight.

Fallbacks: handles dict inputs {key, score/content}, empty lists, equal scores.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


def _extract_pairs(cands) -> List[Tuple[str, float]]:
    """Normalize candidates to list of (key, score) tuples."""
    if not cands:
        return []
    out: List[Tuple[str, float]] = []
    for item in cands:
        try:
            if isinstance(item, dict):
                key = item.get("key") or item.get("id") or item.get("doc_id") or ""
                # score may be under score, relevance_score, _score
                sc = item.get("score")
                if sc is None:
                    sc = item.get("relevance_score", item.get("_score", 0.0))
                out.append((str(key), float(sc) if sc is not None else 0.0))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                k, s = item[0], item[1]
                out.append((str(k), float(s) if s is not None else 0.0))
            else:
                # unsupported shape skip
                continue
        except Exception:
            continue
    return out


def rank_fusion(bm25_cands, vec_cands, k: int = 60) -> List[Tuple[str, float]]:
    """Fuse BM25 and vector candidates via RRF (k) + normalized cosine with 0.5/0.5 weights.

    Args:
        bm25_cands: iterable of (key, score) or dict; ranking derived from score descending
        vec_cands: iterable of (key, cosine_score)
        k: RRF rank constant (default 60)

    Returns:
        List[(key, hybrid_score)] sorted descending by hybrid_score, tie-breaker lexical.
        Empty if both inputs empty.
    """
    try:
        k = int(k)
    except Exception:
        k = 60
    if k <= 0:
        k = 60

    bm25_pairs = _extract_pairs(bm25_cands)
    vec_pairs = _extract_pairs(vec_cands)

    if not bm25_pairs and not vec_pairs:
        return []

    # Sort each list by score descending to derive rank (stable)
    # If scores equal, preserve original order.
    def _sorted_by_score(pairs: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
        # enumerate to keep stable ties
        indexed = list(enumerate(pairs))
        indexed.sort(key=lambda x: (-x[1][1], x[0]))
        return [p for _, p in indexed]

    bm25_sorted = _sorted_by_score(bm25_pairs) if bm25_pairs else []
    vec_sorted = _sorted_by_score(vec_pairs) if vec_pairs else []

    # RRF aggregation: 1/(k+rank), rank 1-indexed
    rrf: Dict[str, float] = {}
    for rank, (key, _sc) in enumerate(bm25_sorted, start=1):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (k + rank)
    for rank, (key, _sc) in enumerate(vec_sorted, start=1):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (k + rank)

    # cosine map
    cos_map: Dict[str, float] = {}
    for key, sc in vec_pairs:
        # keep max if duplicate keys
        if key in cos_map:
            if float(sc) > cos_map[key]:
                cos_map[key] = float(sc)
        else:
            cos_map[key] = float(sc)

    # normalize RRF to [0,1] by max
    max_rrf = max(rrf.values()) if rrf else 0.0
    # normalize cosine by max (avoid min-max to keep semantics)
    max_cos = max(cos_map.values()) if cos_map else 0.0
    # If all cosine scores <=0, treat max_cos as 0 -> norm 0

    hybrid: Dict[str, float] = {}
    all_keys = set(rrf.keys()) | set(cos_map.keys())
    for key in all_keys:
        r = rrf.get(key, 0.0)
        r_norm = (r / max_rrf) if max_rrf > 0 else 0.0
        c = cos_map.get(key, 0.0)
        # clip cosine to [0,1] after normalization to avoid negative contribution
        if max_cos > 0:
            c_norm = c / max_cos
        else:
            c_norm = 0.0
        if c_norm < 0:
            c_norm = 0.0
        if c_norm > 1:
            c_norm = 1.0
        # uniform 0.5/0.5
        hybrid[key] = 0.5 * r_norm + 0.5 * c_norm

    # Sort by hybrid desc, tie-breaker lexical for determinism
    ranked = sorted(hybrid.items(), key=lambda x: (-x[1], x[0]))
    return ranked
