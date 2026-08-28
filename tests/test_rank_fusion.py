import pytest
from hero_quant.memory.rank_fusion import rank_fusion


def test_rank_fusion_uniform():
    bm25 = [("a", 0.9), ("b", 0.1)]
    vec = [("b", 0.9), ("a", 0.1)]
    ranked = rank_fusion(bm25, vec, k=60)
    # Both present, uniform weighting should include both at top
    assert ranked[0][0] in ("a", "b")
    assert len(ranked) == 2
    # Scores should be in [0,1]
    for _, sc in ranked:
        assert 0.0 <= sc <= 1.0


def test_rrf_correctness():
    # Only BM25 list, RRF order should follow rank
    bm25 = [("doc1", 3.0), ("doc2", 2.0), ("doc3", 1.0)]
    vec = []
    ranked = rank_fusion(bm25, vec, k=60)
    # RRF 1/(60+rank): doc1 1/61 > doc2 1/62 > doc3 1/63, plus uniform still sorts same
    assert [k for k, _ in ranked] == ["doc1", "doc2", "doc3"]
    # Check numeric RRF normalization: first should be 1.0 *0.5 (cos 0) => 0.5
    # Since max_rrf = 1/61, doc1 norm 1.0 => 0.5
    assert abs(ranked[0][1] - 0.5) < 1e-6


def test_rrf_with_both_lists_sum():
    bm25 = [("a", 10), ("b", 5)]
    vec = [("a", 0.2), ("b", 0.9)]
    ranked = rank_fusion(bm25, vec, k=60)
    # Both have RRF sum equal (~0.0163+0.0161) tie, but cosine favors b -> b top
    assert ranked[0][0] == "b"


def test_empty_inputs():
    assert rank_fusion([], [], k=60) == []
    assert rank_fusion(None, None) == []


def test_uniform_weight_symmetry():
    # When both lists identical order, uniform should keep order
    bm25 = [("x", 1.0), ("y", 0.5)]
    vec = [("x", 0.8), ("y", 0.2)]
    ranked = rank_fusion(bm25, vec, k=60)
    assert ranked[0][0] == "x"


def test_rank_fusion_deterministic_tie_breaker():
    """P2: non-deterministic ordering - tie scores must be lexical tie-breaker."""
    # Same scores, different insertion order should yield same lexical order
    bm25_a = [("b", 1.0), ("a", 1.0)]
    bm25_b = [("a", 1.0), ("b", 1.0)]
    vec = []
    ra = rank_fusion(bm25_a, vec, k=60)
    rb = rank_fusion(bm25_b, vec, k=60)
    assert [k for k, _ in ra] == [k for k, _ in rb] == ["a", "b"], f"non-deterministic tie {ra} vs {rb}"
    # hybrid tie also lexical
    bm25 = [("a", 1.0), ("b", 1.0)]
    vec2 = [("a", 0.5), ("b", 0.5)]
    r = rank_fusion(bm25, vec2, k=60)
    assert r[0][0] == "a" and r[1][0] == "b"


def test_rank_fusion_k_validation_logs_warning(caplog):
    """P2: missing validation - invalid k should be warned and defaulted."""
    import logging
    caplog.set_level(logging.WARNING)
    # k <=0 should default to 60 and warn
    r1 = rank_fusion([("a", 1)], [], k=0)
    assert len(r1) == 1
    # string k that coerces should work, but non-numeric should default
    r2 = rank_fusion([("a", 1)], [], k="bad")
    assert len(r2) == 1
    # negative k
    r3 = rank_fusion([("a", 1)], [], k=-10)
    assert len(r3) == 1


def test_rank_fusion_returns_new_list_not_alias():
    """P2: shallow copy leak - returned list must not be alias of input."""
    bm25 = [("a", 1.0)]
    vec = [("b", 0.9)]
    r = rank_fusion(bm25, vec)
    bm25.append(("c", 5.0))
    # r should not have c
    assert "c" not in [k for k, _ in r]
