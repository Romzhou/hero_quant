# hero-quant 优化设计 · 2026-08-28

> 基准：`docs/project-evaluation-selfcheck-2026-08-28.md`（≈7.6/7.8）→ 目标 8.0 面试硬答  
> 约束：面试叙事拉满 + 3-4天完整 + Cohere API 重排 + 多 Provider + 持久化做透  
> 路径：A 纵向切片 4 Waves（已获 5 段设计审阅认可）

---

## 1. 架构总览

```
Wave 1 (0.8d)  多 Provider LLM 适配器抽层
  ↓ 可演示：真流式 + 超时/重试可度量
Wave 2 (1d)    RAG 统一融合 + Cohere 重排 + golden 评测
  ↓ 可演示：recall@k 数据驱动、权重不再拍脑袋
Wave 3 (1d)    billing / checkpoint PG 持久化 + RLS + 迁移
  ↓ 可演示：重启不丢、跨租户隔离
Wave 4 (0.8d)  记忆接线 + 前端去 mock + SKILL 实弹 + 文档诚信化
  ↓ 可演示：端到端真实产物 + 文档零失修
```

每波独立可演示、可回滚，依托已有 `AgentLoop 11控制点`、`CircuitBreaker`、`TraceWriter`、`PostgresSaver` 骨架。

---

## 2. 组件与职责

| Wave | 新增/改动 | 职责 |
|------|-----------|------|
| 1 | `src/hero_quant/llm/client.py` 新建 | 统一 `timeout=30s` + 指数退避重试 3 次 + `usage` 捕获 + key 轮换；封装 `stream_chat/invoke/chat/__call__` 鸭子分发 |
| 1 | `src/hero_quant/llm/factory.py` | 由“仅选型”升级为真路由 `HERO_LLM_PROVIDER -> {openai, deepseek, anthropic}` |
| 1 | `src/hero_quant/api/server.py:326` | 内联 `ChatOpenAI` 改调 `LLMClient`，`agent/embed.py:232` 补 `timeout` |
| 2 | `src/hero_quant/memory/rank_fusion.py` 新建 | 抽统一 `rank_fusion(bm25, vec) -> RRF(k=60) + 归一 0.5*rrf+0.5*cos`，替换 `store.py:1294` 与 `router.py:315` 两处不一致权重 |
| 2 | `src/hero_quant/memory/rerank.py` 新建 | `CohereReranker` 封装 `POST https://api.cohere.ai/v1/rerank` 超时 5s，失败降级本地融合并 `rerank_fallback_total` 计数 |
| 2 | `tests/test_retrieval_eval.py` + `tests/data/golden_retrieval.jsonl` | 30 对 golden 跑 `recall@5/MRR`，CI 低频门 |
| 3 | `src/hero_quant/checkpoint/postgres.py` | 默认从 `memory://` 切 PG，主路径 PG，`migrations/001_checkpoint.sql` DDL + `expires_at 7d` |
| 3 | `src/hero_quant/billing/service.py` | 内存态切 `asyncpg` PG + RLS 策略 `migrations/002_billing_rls.sql` |
| 4 | `src/hero_quant/agent/loop.py` + `agent/context.py:208` | 每轮 `MemoryStore.recall` 注入 + `writeback`，`skills_digest` 透传 |
| 4 | `frontend/src/pages/Research.tsx:121,178` `Dashboard.tsx` `Risk.tsx` | 去 bench 随机、去热力硬编码、去资产静态，接真实 `positions.csv/metrics.json/tearsheet.html` + 新增 `api/risk.py` |
| 4 | `skills/research|quant/SKILL.md` `memory/ingest.py` | 补实弹技能 + 长文档 512 重叠 64 切块管线 |
| 4 | `README.md` `CHANGELOG.md` `.env.example` | 徽章 277、七路由、补 env、去夸大、改 live 默认 |

YAGNI 边界：持久化做透但迁移仅两条 SQL，不做分库分表；重排仅 Cohere 单模型，不做多重排对比；前端 Risk 仅一版真实汇总，不做复杂可视化。

---

## 3. 数据流

```
用户 q
  → GET /v1/query/stream 验票 issue/consume_ticket
  → LLMFactory 选模型 (stage→deep/quick)
  → llm/client 流式调用 (30s 超时 / 3 次退避 / usage 捕获)
  → AgentLoop 11 控制点
      ├─ 每轮 MemoryStore.recall(namespace) 融合注入
      ├─ tools: get_market_data (已诚实 live) / run_backtest
      ├─ Grounding 8类掩码 + BudgetBreaker $5/日
      └─ Context 0.8 阈值折叠
  → Cohere rerank 精排 TopK (失败→本地 RRF 降级)
  → TraceWriter tmp→fsync→hardlink 侧车 + SSE delta
  → billing/checkpoint 同步 PG (失败→内存回退 + /ready degraded)
```

离线：`FakeLLM` + `MiniLM` + 本地 RRF 降级链，保证无 key 可演示。

---

## 4. 错误处理

| 场景 | 策略 | 可观测 |
|------|------|--------|
| LLM 超时/5xx | 指数退避 1s·2^n+jitter 重试 3 次 → 切 quick 模型 | `trace.reason=llm_timeout` + `metrics llm_retry_total` |
| Cohere 失败 | 静默降级本地 `rank_fusion` | `rerank_fallback_total` + `logger.warning` |
| PG 不可用 | 回退内存态 | `/ready {status: "degraded", pg:false}` + `logger.warning` |
| 数据 live 失败 | 显式 `RuntimeError` 不合成 | 前端 toast + trace 记录 |
| 跨源 1% | 抛 `CrossSourceError` 阻断 | metrics + ledger |
| 鉴权 | 保持现状 prefix 检查，仅补日志；P2 再做 JWT | — |

原则：主链路 fail-closed 可追溯，降级路径可度量。

---

## 5. 测试策略（TDD 强制）

每任务：先写失败测试 → 观察 FAIL → 最小实现 → 观察 PASS → 提交。

- Wave1: `tests/test_llm_client.py` 覆盖超时/重试/多 Provider/usage
- Wave2: `tests/test_rank_fusion.py` + `tests/test_rerank.py` + `tests/test_retrieval_eval.py`（30 golden，断言 `recall@5 ≥0.80` 且 `rerank` 相对提升 `+5%`）
- Wave3: `tests/test_checkpoint_pg.py` + `tests/test_billing_persistence.py`（`testcontainers[postgres]` 隔离，断言重启不丢、RLS 隔离 0 行越权）
- Wave4: `tests/test_memory_loop_inject.py` + `tests/test_ingest.py` + `frontend/src/__tests__/**` + 新增 `e2e/Chat.spec.ts` Playwright（流式 + Research 渲染 + SPA）

覆盖率：`--cov-fail-under` 46 → 50（Wave2 后）→ 55（收口），分阶段提升避免一次性卡门。

---

## 6. 风险与回滚

- Cohere key 缺失：CI 跳过重排测试，本地降级保证不阻塞
- PG 容器不可用：fallback 内存，`testcontainers` 仅本地/CI 启用
- 多 Provider 密钥：`HERO_LLM_PROVIDER` 切换失败回退 `openai`
- 每 Wave 独立分支可 `git revert`，Wave 间无强耦合

---

*设计审阅：5/5 段已认可（2026-08-28）。下一步 → `docs/plans/2026-08-28-hero-quant-optimize.md` 实施计划。*
