"""Uniform RRF + cosine fusion (0.5/0.5).

Provides rank_fusion(bm25_cands, vec_cands, k=60) -> List[Tuple[str, float]]
where inputs are iterables of (key, score). RRF derived from rank order
(sorted desc by score), cosine normalized by max, uniform 0.5 weight.

Fallbacks: handles dict inputs {key, score/content}, empty lists, equal scores.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# 统一融合常量（PR2-F）：router 与 store 双轨统一于此 —— RRF k=60 + 0.5/0.5 归一语义。
RRF_K = 60
W_RRF = 0.5
W_COS = 0.5


def _extract_pairs(cands) -> List[Tuple[str, float]]:
    """Normalize candidates to list of (key, score) tuples."""
    if not cands:
        return []
    out: List[Tuple[str, float]] = []
    for item in cands:
        try:
            if isinstance(item, dict):
                # explicit None/"" check - avoid collapsing to ""
                key = item.get("key")
                if key is None or (isinstance(key, str) and key == ""):
                    key = item.get("id")
                if key is None or (isinstance(key, str) and key == ""):
                    key = item.get("doc_id")
                if key is None or (isinstance(key, str) and key == ""):
                    # 全缺 continue 不落 ""
                    continue
                # score may be under score, relevance_score, _score
                sc = item.get("score")
                if sc is None:
                    sc = item.get("relevance_score", item.get("_score", 0.0))
                out.append((str(key), float(sc) if sc is not None else 0.0))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                k, s = item[0], item[1]
                if k is None or (isinstance(k, str) and k == ""):
                    continue
                out.append((str(k), float(s) if s is not None else 0.0))
            else:
                # unsupported shape skip
                continue
        except (ValueError, TypeError):  # narrow: only conversion/type errors
            continue
    return out


def _dedup_max(pairs: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Deduplicate by key keeping max score per key."""
    best: Dict[str, float] = {}
    for k, s in pairs:
        if k not in best or s > best[k]:
            best[k] = s
    return list(best.items())


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
    import logging
    logger = logging.getLogger("hero_quant.memory.rank_fusion")
    orig_k = k
    try:
        k = int(k)
    except (ValueError, TypeError):  # narrow: only conversion errors
        logger.warning("rank_fusion invalid k %r, defaulting to 60", orig_k)
        k = 60
    if k <= 0:
        # P2: k<=0 为非法参数 —— 兼容历史测试的 warning 回落，同时显式告警避免静默掩盖
        # 后续可收紧为 raise ValueError；当前保留 warning+回落以保证存量测试通过
        logger.warning("rank_fusion invalid k %r, defaulting to 60", orig_k)
        k = 60

    # 先 _dedup_max 每 key 最高分 再 RRF
    bm25_pairs = _dedup_max(_extract_pairs(bm25_cands))
    vec_pairs = _dedup_max(_extract_pairs(vec_cands))

    if not bm25_pairs and not vec_pairs:
        return []

    # Sort each list by score descending to derive rank (deterministic)
    # If scores equal, lexical tie-breaker for determinism (not insertion order).
    def _sorted_by_score(pairs: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
        # deterministic: sort by (-score, key) lexical
        return sorted(pairs, key=lambda kv: (-kv[1], kv[0]))

    bm25_sorted = _sorted_by_score(bm25_pairs) if bm25_pairs else []
    vec_sorted = _sorted_by_score(vec_pairs) if vec_pairs else []

    # RRF aggregation: 1/(k+rank), rank 1-indexed
    rrf: Dict[str, float] = {}
    for rank, (key, _sc) in enumerate(bm25_sorted, start=1):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (k + rank)
    for rank, (key, _sc) in enumerate(vec_sorted, start=1):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (k + rank)

    # cosine map 用 dedup 结果
    cos_map: Dict[str, float] = dict(vec_pairs)

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
        # 中文注释：缺失向量的 key 不赋 0.5 假余弦，避免 BM25-only 白嫖分数
        if key not in cos_map:
            c_norm = 0.0
        elif max_cos > 0:
            c_raw = cos_map[key] / max_cos
            c_raw = max(-1.0, min(1.0, c_raw))
            c_norm = (c_raw + 1.0) / 2.0
        else:
            c_norm = 0.0
        # uniform 0.5/0.5
        hybrid[key] = W_RRF * r_norm + W_COS * c_norm

    # Sort by hybrid desc, tie-breaker lexical for determinism
    ranked = sorted(hybrid.items(), key=lambda x: (-x[1], x[0]))
    return ranked


def bm25_from_ordered(bm25_items) -> List[Tuple[str, float]]:
    """将有序 BM25 候选（dict 含 key / 或 (key, score)）转为融合输入。

    router/store 双轨统一于此：按出现顺序赋递减分，保留召回排序信息。
    """
    pairs: List[Tuple[str, float]] = []
    try:
        items = list(bm25_items) if bm25_items else []
    except TypeError:
        return []
    n = len(items)
    for idx, it in enumerate(items):
        try:
            if isinstance(it, dict):
                key = it.get("key")
                if key is None or (isinstance(key, str) and key == ""):
                    key = it.get("id")
                if key is None or (isinstance(key, str) and key == ""):
                    key = it.get("doc_id")
                if key is None or (isinstance(key, str) and key == ""):
                    continue
                pairs.append((str(key), float(n - idx)))
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                k = it[0]
                if k is None or (isinstance(k, str) and k == ""):
                    continue
                pairs.append((str(k), float(n - idx)))
        except (ValueError, TypeError):
            continue
    return pairs


def vec_from_scored(vec_items) -> List[Tuple[str, float]]:
    """将向量候选（dict 含 key/score / 或 (key, score)）转为融合输入。

    score 缺失时取 0.0；rank_fusion 内部做 max 归一，调用方无需预归一。
    """
    return _extract_pairs(vec_items)


def fuse(bm25_cands, vec_cands, k: int = RRF_K) -> List[Tuple[str, float]]:
    """统一融合入口（PR2-F）：RRF(k=60) + 归一 0.5*RRF + 0.5*cosine。

    router（工具路由）与 store（记忆检索）均经此入口，消除双轨权重。
    输入形态与 rank_fusion 一致：(key, score) 元组或 dict 列表。
    dict 形态下 bm25 侧按出现顺序赋分（见 bm25_from_ordered），与历史 store 语义一致。
    """
    try:
        kk = int(k)
    except (ValueError, TypeError):
        kk = RRF_K
    if kk <= 0:
        kk = RRF_K
    bm25_pairs = bm25_cands
    # 中文注释：完整检测 dict 形态，兼容 generator/混合类型
    try:
        peek = list(bm25_cands) if bm25_cands else []
    except TypeError:
        peek = []
    if peek and all(isinstance(x, dict) for x in peek):
        # Preserve explicit scores when present; only fall back to order scores
        # when dicts carry no usable score field.
        def _has_score(d):
            return d.get("score") is not None or d.get("relevance_score") is not None or d.get("_score") is not None
        if all(_has_score(d) for d in peek):
            bm25_pairs = peek
        else:
            bm25_pairs = bm25_from_ordered(peek)
    else:
        # reuse materialized peek; original iterator is already consumed
        bm25_pairs = peek
    return rank_fusion(bm25_pairs, vec_cands, k=kk)
