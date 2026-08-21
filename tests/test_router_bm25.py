"""B3-1 BM25 router TDD — momentum factor 首位 compute_factor 且 score 为 BM25 非固定 +3."""
from __future__ import annotations

import math
import re
from collections import Counter


def _local_tokenize(text: str):
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _expected_bm25(query: str, tool_name: str) -> float:
    from hero_quant.tools.registry import TOOL_REGISTRY

    K1 = 1.5
    B = 0.75
    corpus = [_local_tokenize((spec.description or "")) for spec in TOOL_REGISTRY.values()]
    N = len(corpus)
    df: dict[str, int] = {}
    for doc in corpus:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    idf = {term: math.log((N - freq + 0.5) / (freq + 0.5) + 1) for term, freq in df.items()}
    avg_dl = sum(len(d) for d in corpus) / N if N else 0.0
    qt = _local_tokenize(query)
    spec = TOOL_REGISTRY.get(tool_name)
    doc_tokens = _local_tokenize((spec.description or "") if spec else "")
    dl = len(doc_tokens)
    if dl == 0 or avg_dl == 0:
        return 0.0
    tf_map = Counter(doc_tokens)
    score = 0.0
    seen = set()
    for term in qt:
        if term in seen:
            continue
        seen.add(term)
        _idf = idf.get(term, 0.0)
        if _idf <= 0:
            continue
        tf = tf_map.get(term, 0)
        if tf == 0:
            continue
        numerator = tf * (K1 + 1)
        denominator = tf + K1 * (1 - B + B * dl / avg_dl)
        score += _idf * numerator / denominator
    return score


def test_route_momentum_factor_top1_is_compute_factor():
    from hero_quant.mcp.router import route

    top = route("momentum factor", k=5)
    assert len(top) == 5
    assert top[0] == "compute_factor", f"expected compute_factor首位, got {top}"


def test_score_uses_bm25_not_fixed_plus3():
    """score 必须用 BM25(K1=1.5 B=0.75) idf log((N-n+0.5)/(n+0.5)+1)，非固定 +3/+1."""
    from hero_quant.mcp.router import _score_tool
    from hero_quant.tools.registry import TOOL_REGISTRY

    query = "momentum factor"
    qt = _local_tokenize(query)
    expected = _expected_bm25(query, "compute_factor")
    actual = _score_tool(qt, query.lower(), "compute_factor", TOOL_REGISTRY["compute_factor"].description)
    # BM25 分数与手工计算一致（误差 1e-6）
    assert abs(actual - expected) < 1e-6, f"BM25 expected {expected:.6f}, got {actual:.6f}"
    # 非固定 +3/+1/+10 旧逻辑会得到整数且>10，BM25 为小浮点
    assert actual < 10, f"score should be BM25 small value, got {actual}"
    # 旧实现对 momentum query 会加 +10 导致 >=10，BM25 不会
    assert actual != 13.0 and actual != 14.0 and actual != 10.0


def test_bm25_idf_formula_and_constants():
    """验证 idf = log((N-n+0.5)/(n+0.5)+1) 且 K1=1.5 B=0.75 生效."""
    from hero_quant.mcp import router

    # 常量应暴露或可推断
    assert hasattr(router, "_BM25_K1") and router._BM25_K1 == 1.5
    assert hasattr(router, "_BM25_B") and router._BM25_B == 0.75
    # avg_dl / idf 预计算应存在
    # 触发一次 route 以确保语料已构建
    router.route("test", k=1)
    assert hasattr(router, "_AVG_DL") or hasattr(router, "_avg_dl") or hasattr(router, "AVG_DL")
    # 检查 idf 存在且用正确公式计算（抽查一个词）
    from hero_quant.tools.registry import TOOL_REGISTRY

    corpus = [_local_tokenize((s.description or "")) for s in TOOL_REGISTRY.values()]
    N = len(corpus)
    # 选一个已知词：factor
    n_factor = sum(1 for d in corpus if "factor" in d)
    expected_idf = math.log((N - n_factor + 0.5) / (n_factor + 0.5) + 1)
    # 从 router 取 idf
    idf_map = getattr(router, "_IDF", None) or getattr(router, "_idf", None) or getattr(router, "IDF", None)
    if idf_map is None:
        # 尝试通过 _get_idf 等
        idf_map = {}
        for attr in dir(router):
            if "idf" in attr.lower():
                v = getattr(router, attr)
                if isinstance(v, dict) and "factor" in v:
                    idf_map = v
                    break
    assert "factor" in idf_map, f"idf should contain 'factor', got keys {list(idf_map.keys())[:5]}"
    assert abs(idf_map["factor"] - expected_idf) < 1e-6
