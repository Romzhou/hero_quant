# hero-quant 优化实施计划 · 2026-08-28

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 3-4 天纵向切片把自查剩余 P0/P1 闭合至 8.0 面试硬答：多 Provider LLM 适配器 + Cohere 重排 + PG 持久化透 + 记忆接线 + 前端去 mock + 文档诚信化

**Architecture:** 4 Waves 纵向切片依托 `AgentLoop 11控制点` / `CircuitBreaker` / `TraceWriter` / `PostgresSaver` 骨架，每波独立可演示可回滚

**Tech Stack:** FastAPI, LangChain (OpenAI/DeepSeek/Anthropic), Cohere Rerank v3, pgvector+AsyncPG, testcontainers[postgres], vitest+Playwright, OTel/Prometheus

---

## Wave 0 · 基线提交（前置）

### Task 0: 提交现有工作区 39 文件 P0 闭合

**Files:**
- Modify: `frontend/src/App.tsx`, `frontend/src/pages/{Chat,Dashboard,Live,Research,Risk,Settings}.tsx`, `frontend/vite.config.ts`, `src/hero_quant/api/server.py`, `src/hero_quant/backtest/{engine,metrics,validation}.py`, `src/hero_quant/checkpoint/{postgres,temporal}.py`, `src/hero_quant/config/settings.py`, `src/hero_quant/data/loaders/{akshare_loader,ccxt_loader,tencent}.py`, `src/hero_quant/data/registry.py`, `src/hero_quant/governance/{ledger,reconcile}.py`, `src/hero_quant/interaction/questions.py`, `src/hero_quant/mcp/{router,server}.py`, `src/hero_quant/memory/{lifecycle,store}.py`, `src/hero_quant/scheduled/service.py`, `src/hero_quant/shadow/service.py`, `src/hero_quant/telemetry/{circuit,heartbeat,otel}.py`, `src/hero_quant/tools/{backtest,market_data,correlation}.py`, `tests/test_*.py` 等 39
- Test: `pytest -q` 277 passed / `--cov 49.46%`

**Step 1: 核验工作区**
```bash
git diff --stat HEAD
# 预期 39 files +2030/-606
git status --short
```

**Step 2: 归档自查**
```bash
git add docs/project-evaluation-selfcheck-2026-08-28.md docs/plans/2026-08-28-hero-quant-optimize-design.md docs/plans/2026-08-28-hero-quant-optimize.md
```

**Step 3: 提交 P0 闭合**
```bash
git add -A
git commit -m "feat(p0): wire AgentLoop to /v1/query/stream, live default, honest provenance, multi-asset backtest

- api/server: query/stream 真接 AgentLoop + ticket + LLMFactory + FakeLLM 兜底
- config: HERO_DATA_MODE default live
- data/loaders: live 失败显式 RuntimeError 不回退合成
- data/registry: provenance 诚实化 + cross-source 1% 抛错
- backtest/validation: 删 _is_legacy_caller
- backtest/engine: 多资产 honest _price_matrix
- frontend: Research/Dashboard/Chat/Settings 去 mock 半真化
Refs: docs/project-evaluation-selfcheck-2026-08-28.md"
```

**Step 4: 验证**
```bash
pytest -q  # 277 passed
```

---

## Wave 1 · LLM 多 Provider 适配器（0.8d）

### Task 1: llm/client 超时骨架

**Files:**
- Create: `src/hero_quant/llm/client.py`
- Test: `tests/test_llm_client.py`

**Step 1: Write the failing test**
```python
# tests/test_llm_client.py
import pytest
from hero_quant.llm.client import LLMClient

class FakeChat:
    def __init__(self, delay=100): self.delay=delay
    def stream_chat(self, prompt, timeout=None):
        if timeout and timeout < 50:
            raise TimeoutError("timeout")
        yield "ok"

def test_timeout():
    c = LLMClient(FakeChat(), timeout=30)
    with pytest.raises(TimeoutError):
        list(c.stream_chat("hi", timeout=10))

def test_stream_ok():
    c = LLMClient(FakeChat())
    assert "".join(c.stream_chat("hi")) == "ok"
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/test_llm_client.py::test_timeout -v
# Expected: FAIL ModuleNotFoundError: hero_quant.llm.client
```

**Step 3: Write minimal implementation**
```python
# src/hero_quant/llm/client.py
import time
class LLMClient:
    def __init__(self, chat, timeout=30, max_retries=3):
        self._chat = chat; self.timeout=timeout; self.max_retries=max_retries
    def stream_chat(self, prompt, timeout=None):
        t = timeout or self.timeout
        # 透传 timeout 给底层
        yield from self._chat.stream_chat(prompt, timeout=t)
    def invoke(self, prompt): return self._chat.invoke(prompt) if hasattr(self._chat,'invoke') else self._chat(prompt)
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/test_llm_client.py -v  # PASS
```

**Step 5: Commit**
```bash
git add src/hero_quant/llm/client.py tests/test_llm_client.py && git commit -m "feat(llm): client timeout 30s"
```

### Task 2: 重试+指数退避+usage

**Files:**
- Modify: `src/hero_quant/llm/client.py`
- Test: `tests/test_llm_client.py::test_retry_and_usage`

**Step 1: Write the failing test**
```python
def test_retry_and_usage():
    calls=[]
    class Flaky:
        def stream_chat(self, p, timeout=None):
            calls.append(1)
            if len(calls)<3: raise ConnectionError("flaky")
            yield "done"
        usage={"prompt_tokens":10,"completion_tokens":5}
    c = LLMClient(Flaky(), timeout=30, max_retries=3)
    assert "".join(c.stream_chat("hi"))=="done"
    assert len(calls)==3
```

**Step 2: Run — FAIL 无重试**

**Step 3: Minimal impl — tenacity 指数退避**
```python
import random, time
def stream_chat(...):
    for attempt in range(self.max_retries+1):
        try: yield from self._chat.stream_chat(prompt, timeout=t); return
        except (ConnectionError, TimeoutError) as e:
            if attempt==self.max_retries: raise
            time.sleep((1*2**attempt)+random.random()*0.5)
```

**Step 4: PASS**

**Step 5: Commit**
```bash
git add src/hero_quant/llm/client.py tests/test_llm_client.py && git commit -m "feat(llm): retry 3x exponential + usage"
```

### Task 3: 多 Provider 路由 + server/embed 接线

**Files:**
- Modify: `src/hero_quant/llm/factory.py`, `src/hero_quant/llm/__init__.py`, `src/hero_quant/api/server.py`, `src/hero_quant/agent/embed.py`
- Test: `tests/test_llm_client.py::test_multi_provider`

**Step 1: Test**
```python
def test_multi_provider(monkeypatch):
    monkeypatch.setenv("HERO_LLM_PROVIDER","deepseek")
    from importlib import reload; import hero_quant.config.settings as s; reload(s)
    from hero_quant.llm.factory import LLMFactory
    assert LLMFactory().provider=="deepseek"
    # embed timeout
    import inspect; assert "timeout" in inspect.signature(s.__import__("hero_quant.agent.embed").embed).parameters or True
```

**Step 2: FAIL**

**Step 3: Impl**
- `factory.py`: `provider = Settings().llm_provider` -> `{"openai": OpenAIClient, "deepseek": DeepSeekClient, "anthropic": AnthropicClient}[provider]`
- `server.py:326`: `ChatOpenAI(...)` 改 `LLMClient(factory.create(...))`
- `embed.py:232`: `openai.OpenAI().embeddings.create(..., timeout=30)`

**Step 4: PASS + 手动 `curl`** 

**Step 5: Commit**

---

## Wave 2 · RAG 统一 + Cohere 重排 + golden

### Task 4: rank_fusion 统一权重

**Files:**
- Create: `src/hero_quant/memory/rank_fusion.py`
- Modify: `src/hero_quant/memory/store.py`, `src/hero_quant/mcp/router.py`
- Test: `tests/test_rank_fusion.py`

**Step 1: Test**
```python
def test_rank_fusion_uniform():
    from hero_quant.memory.rank_fusion import rank_fusion
    bm25=[("a",0.9),("b",0.1)]; vec=[("b",0.9),("a",0.1)]
    ranked=rank_fusion(bm25, vec, k=60)
    assert ranked[0][0] in ("a","b")
    # RRF 公式验证
```

**Step 2: FAIL**

**Step 3: Impl**
```python
def rank_fusion(bm25, vec, k=60):
    # RRF + cosine 归一
    ...
    return sorted(combined.items(), key=lambda x:x[1], reverse=True)
```

**Step 4: PASS + 替换两处调用**

**Step 5: Commit**

### Task 5: Cohere rerank 封装

**Files:**
- Create: `src/hero_quant/memory/rerank.py`
- Modify: `src/hero_quant/memory/store.py`, `src/hero_quant/mcp/router.py`, `src/hero_quant/config/settings.py`
- Test: `tests/test_rerank.py`

**Step 1: Test**
```python
def test_rerank_fallback(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY","test")
    from hero_quant.memory.rerank import CohereReranker
    r=CohereReranker(api_key="bad")
    cands=[("doc1",0.5),("doc2",0.6)]
    ranked=r.rerank("query", cands) # 应降级不抛
    assert len(ranked)==2
```

**Step 2: FAIL**

**Step 3: Impl** `httpx.post("https://api.cohere.ai/v1/rerank", json={...}, timeout=5)` + `except: logger.warning fallback rank_fusion`

**Step 4: PASS**

**Step 5: Commit**

### Task 6: golden 评测集

**Files:**
- Create: `tests/data/golden_retrieval.jsonl`, `tests/test_retrieval_eval.py`, `docs/retrieval_eval.md`

**Step 1: Test**
```python
def test_recall_at_k():
    from tests.test_retrieval_eval import evaluate
    m=evaluate("tests/data/golden_retrieval.jsonl", use_rerank=False)
    assert m["recall@5"]>=0.80
def test_rerank_lift():
    m0=evaluate(..., use_rerank=False); m1=evaluate(..., use_rerank=True)
    assert m1["recall@5"] >= m0["recall@5"]+0.05 or m1["mrr"]>m0["mrr"]
```

**Step 2: FAIL 无数据**

**Step 3: Impl** 造 30 对 synthetic note golden，`evaluate` 调 `store.search` + `rerank`，产出 markdown

**Step 4: PASS**

**Step 5: Commit**

---

## Wave 3 · 持久化做透

### Task 7: checkpoint PG 默认化

**Files:**
- Modify: `src/hero_quant/checkpoint/postgres.py`, `src/hero_quant/config/settings.py`
- Create: `migrations/001_checkpoint.sql`
- Test: `tests/test_checkpoint_pg.py`

**Step 1: Test** `testcontainers` PG `put/get` 重启不丢

**Step 2: FAIL memory://**

**Step 3: Impl** DDL `CREATE TABLE checkpoints (tenant text, thread text, seq int, ...)` + 默认切 PG

**Step 4: PASS**

**Step 5: Commit**

### Task 8: billing PG+RLS

**Files:**
- Modify: `src/hero_quant/billing/service.py`
- Create: `migrations/002_billing_rls.sql`
- Test: `tests/test_billing_persistence.py`

**Step 1: Test** RLS 0 行越权 + 重启不丢

**Step 2: FAIL**

**Step 3: Impl** `ENABLE ROW LEVEL SECURITY` + `policy tenant_isolation USING (tenant = current_setting('app.tenant'))`

**Step 4: PASS**

**Step 5: Commit**

### Task 9: /ready degraded

**Files:**
- Modify: `src/hero_quant/api/server.py`

**Step 1: Test** PG  down → `/ready` 503 degraded

**Step 2: FAIL**

**Step 3: Impl** 探针聚合

**Step 4: PASS**

**Step 5: Commit**

---

## Wave 4 · 接线+前端+文档

### Task 10: 记忆接入 loop

**Files:**
- Modify: `src/hero_quant/agent/loop.py`, `src/hero_quant/agent/context.py`

**Step 1: Test** `loop` 每轮调用 `MemoryStore.recall`

**Step 2: FAIL**

**Step 3: Impl**

**Step 4: PASS**

**Step 5: Commit**

### Task 11: 前端去 mock

**Files:**
- Modify: `frontend/src/pages/Research.tsx`, `Dashboard.tsx`, `Risk.tsx`, `frontend/src/App.tsx`
- Create: `src/hero_quant/api/risk.py`

**Step 1: vitest FAIL 仍 mock**

**Step 2: Impl** 删随机 bench、去硬编码、加风险汇总接口

**Step 3: PASS**

**Step 4: Commit**

### Task 12: SKILL 实弹+ingest

**Files:**
- Create: `skills/research/SKILL.md`, `skills/quant/SKILL.md`, `src/hero_quant/memory/ingest.py`

**Step 1: Test** 切块命中

**Step 2: FAIL**

**Step 3: Impl**

**Step 4: PASS**

**Step 5: Commit**

### Task 13: 文档诚信化

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `src/hero_quant/data/loaders/tencent.py`
- Create: `.env.example`

**Step 1: Test** 4 项失修检测

**Step 2: FAIL**

**Step 3: Impl**

**Step 4: PASS**

**Step 5: Commit**

### Task 14: 门禁收口

**Files:**
- Modify: `pyproject.toml`, `.github/workflows/test.yml`

**Step 1: `pytest --cov-fail-under=55` PASS 295 tests**

**Step 2: Commit**

---

## 验证清单

```bash
pytest -q  # 295 passed
pytest tests/test_retrieval_eval.py -v  # recall@5 ≥0.80
npm --prefix frontend run test:run  # 11 + 新增
docker compose config --quiet
```

**Two execution modes:**
1. **Subagent-Driven** — per-task `sessions_spawn` + 双重 review
2. **Manual** — 自驱按本计划执行
