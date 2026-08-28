import pytest
from hero_quant.memory.rank_fusion import rank_fusion


def test_missing_key_not_collapse():
    a = [{"score": 0.9}, {"score": 0.8}]
    r = rank_fusion(a, [])
    # r is List[Tuple[str,float]] ; should not contain empty key
    if len(r) == 0:
        return
    keys = [k for k, _ in r]
    assert "" not in keys, f"empty key collapsed: {r}"
    # also ensure no empty string key in dict view
    assert all(k != "" for k in keys)


def test_rerank_topk_validation():
    from hero_quant.memory.rerank import CohereReranker

    r = CohereReranker(api_key="dummy-test-key", timeout=5)
    cands = [{"key": f"doc{i}", "content": "hi", "score": 0.5} for i in range(10)]
    with pytest.raises(ValueError):
        r.rerank("hi", cands, top_k=0)
    # also non-numeric and negative should raise
    with pytest.raises(ValueError):
        r.rerank("hi", cands, top_k=-1)
