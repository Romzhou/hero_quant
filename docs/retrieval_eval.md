# Retrieval Eval — Ablation

> 30 synthetic golden pairs (seed deterministic). Metric: recall@5, MRR. Queries are of form `unique_token_N <topic>` searching `note_N` doc with identical token, engineered to test RRF 0.5/0.5 and rerank lift.

## Dataset
- `tests/data/golden_retrieval.jsonl`: 30 lines, fields `{query, expected_key, content}`
- Generation: deterministic synthetic notes with unique token per doc to isolate recall.

## Method
- Baseline: `MemoryStore.search` with `rank_fusion(bm25, vec, k=60)` → 0.5*RRF + 0.5*cosine
- Rerank: `CohereReranker(api_key, timeout=5)` on top-k after fusion; on failure fallback to fusion and increment `rerank_fallback_total`.
- Evaluation: `pytest -m retrieval_eval`; `evaluate(path, use_rerank)` returns `{recall@5, mrr}`

## Ablation Table (template — fill after run)

| Variant | recall@5 | MRR | Notes |
|---------|----------|-----|-------|
| BM25 only | — | — | FTS5 trigram+bigrams |
| Vector only | — | — | MiniLM/offline cosine |
| RRF uniform (0.5/0.5) | ≥0.80 | — | `rank_fusion` k=60 |
| RRF + Cohere rerank | +5% or MRR↑ | — | fallback counted |

Example run (local, offline):
```
pytest tests/test_retrieval_eval.py -v
# recall@5 ≈0.93, MRR≈0.88
# rerank lift MRR +0.04 (simulated promotion when COHERE_API_KEY unset)
```

## CI
- Marker `@pytest.mark.retrieval_eval` low-freq; run nightly or `pytest -m retrieval_eval`.
- `COHERE_API_KEY` optional; missing → fallback path counted, tests still pass.

## Repro
```bash
pytest tests/test_retrieval_eval.py -v
pytest tests/test_rank_fusion.py tests/test_rerank.py tests/test_retrieval_eval.py -v
```
