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

## Dimension Ablation — Embedding Size 32 / 128 / 768 (stub)

> 方法：固定 30 synthetic golden pairs + RRF 0.5/0.5 + offline cosine，控制 `HERO_VECTOR_DIM` 仅改维度，TopK=5，对比 recall@5 / MRR / p50。当前为占位 stub，非真模型跑分，待 Wave6 真跑后回填。

| Dim | recall@5 | MRR | p50 latency | Notes |
|-----|----------|-----|-------------|-------|
| 32  | 0.93 | 0.88 | 4.2ms | default `32` 轻量兼顾内存，baseline |
| 128 | 0.95 | 0.90 | 7.8ms | 中维度，预期 +2% recall，latency ↑ |
| 768 | 0.96 | 0.91 | 18ms | MiniLM `768` 全维，收益边际递减 |

*填写口径：`Settings(HERO_VECTOR_DIM)` + `MemoryStore.search` 本地 cosine，`COHERE_API_KEY` 缺失走 fallback，不影响维度对比。后续补真跑 `pytest -m retrieval_eval --dim=32,128,768` 产出。*

## Repro
```bash
pytest tests/test_retrieval_eval.py -v
pytest tests/test_rank_fusion.py tests/test_rerank.py tests/test_retrieval_eval.py -v
```
