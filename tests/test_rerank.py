import pytest


def test_rerank_fallback(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "test")
    from hero_quant.memory.rerank import CohereReranker, get_fallback_count

    r = CohereReranker(api_key="bad", timeout=1)
    cands = [("doc1", 0.5), ("doc2", 0.6)]
    ranked = r.rerank("query", cands)
    assert len(ranked) == 2
    assert set(k for k, _ in ranked) == {"doc1", "doc2"}
    # fallback should have incremented
    assert get_fallback_count() >= 1


def test_rerank_fallback_no_key():
    from hero_quant.memory.rerank import CohereReranker

    r = CohereReranker(api_key="", timeout=1)
    cands = [("a", 0.9), ("b", 0.1)]
    ranked = r.rerank("hello", cands)
    assert len(ranked) == 2


def test_rerank_handles_dict_input(monkeypatch):
    from hero_quant.memory.rerank import CohereReranker

    r = CohereReranker(api_key="bad", timeout=1)
    cands = [{"key": "doc1", "content": "hello world", "score": 0.5}, {"key": "doc2", "content": "foo", "score": 0.6}]
    ranked = r.rerank("hello", cands)
    assert len(ranked) == 2
