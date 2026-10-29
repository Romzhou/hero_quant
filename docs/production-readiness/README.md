# hero-quant 生产级任务书 · 总纲

> 版本：v1（2026-10-29）· 基线：`7383e4e` · 性质：规划文档，零代码改动
> 适用对象：后续执行的生产化专线（人类工程师 + AI 代理）
> 用法：先读 §1 决策、§2 现状、§3 目标架构，再按 §4 分期推进；任务明细见 `backlog.md`，逐期执行见 `phase-1.md` / `phase-2.md` / `phase-3.md`。

---

## 1. 已定决策（不要重新讨论）

| # | 决策 | 影响 |
|---|---|---|
| D1 | **内部团队工具**：已知用户、需登录与租户隔离；不做开放注册/对外订阅计费 | 身份用企业 SSO/OIDC；Billing 仅内部因子市场，不做支付 |
| D2 | **研究与回测，不接实盘**：不产生交易指令、不输出个性化投资建议 | 无需券商接入/牌照；审批聚焦数据导出与高危工具；合规文本按“研究工具”写 |
| D3 | **自建 K8s + 独立/托管 PG/Redis**：生产部署在自有 K8s 集群，中间件独立部署或托管 | 需 Helm/部署清单、备份/PITR、多环境、灰度回滚；DB/Redis 不与应用同生命周期 |
| D4 | **全功能分期**：Billing / 调度 / RAG / Risk 全部保留，但按 Phase 1→2→3 交付 | 避免首期范围爆炸；每期有独立验收门 |
| D5 | **自建 Keycloak（OIDC）** 作为团队 IdP | Phase 1 部署 Keycloak（Helm），后端只校验 JWT；不接外部 IdP |
| D6 | **多 provider 路由**：OpenAI / DeepSeek / 国内合规模型，按租户/任务路由 | LLM Gateway 需 provider 列表、failover、成本归集；A/B 放 Phase 2 |
| D7 | **数据源：公开源内部用**（akshare/tushare/yahoo），文档写明限制，不对外分发 | 内部研究可用；对外功能前必须采购合规源（Phase 3 PR-322） |

### 1.1 仍需补充的参数（不阻塞开工）

| 参数 | 当前值 | 何时必须定 |
|---|---|---|
| LLM 月度预算上限 | 待定 | Phase 1 第 1 周（影响成本告警阈值） |
| 自建 K8s 的 PG/Redis 形态 | 独立部署或托管，Phase 1 定 | PR-162 开始前 |
| 国内合规模型具体厂商 | 待定（多 provider 列表内可插拔） | PR-111 实现前 |

---

## 2. 现状基线（2026-10-29 实测）

| 项 | 结果 | 证据 |
|---|---|---|
| 功能打通率 | **约 75%** | 12 项加权评分，见 §2.2 |
| 生产就绪度 | **约 55%** | 身份/评测/调度/Billing 入口缺失 |
| 后端测试 | 2761 passed / 54 skipped / 72.14% | worktree 实跑；主仓 2762/53/72.15 |
| 前端单测 | 70 passed / 10 files | `npm run test -- --run` |
| e2e | 19 passed，4 project 全调度 | `npx playwright test` |
| lint | `ruff check src` 全过 | 本轮 |
| 提交 | 领先 origin 72 个，未推送 | `git log origin/master..master` |

### 2.1 已经真实可用的底座（不要重写）

- **问答链**：`/v1/query`、`/v1/query/stream` 真接 `AgentLoop`；`LLMFactory` 可建 OpenAI/DeepSeek 客户端；无 key 时 `_FakeLLM` 降级并标 `degraded`。
- **回测链**：多资产、PIT、成本、`on_bar`/`on_tick`、metrics/positions/tearsheet/drawdowns；真行情实测可取（当前只取 20 根）。
- **记忆/RAG**：FTS5 + 本地向量 + RRF + 可选 Cohere rerank；memory 已接进 loop。
- **持久化**：checkpoint PG、billing PG + RLS；`/ready` 实测 `pg=true`。
- **可观测/安全**：Prometheus、structlog、trace SSE、三态熔断、沙箱、脱敏、供应链 hash-lock。

### 2.2 未打通清单（本任务书要解决的）

| 层 | 缺口 | 现状证据 |
|---|---|---|
| 身份 | 匿名 SSE 票、无用户表/登录/RBAC/租户配额 | `api/security.py:233` |
| 模型 | 无 API key，真模型一次未跑；评测默认 fake，real 结果 `skipped/no_llm_backend` | `evals/results_real.json` |
| 数据 | 只取 20 根；无 PIT 库/快照/复权/幸存者偏差；回测不可复现 | `server.py:_BACKTEST_BARS_TARGET=20` |
| 会话 | 无 conversation/session store；工具调用无幂等键 | `grep` 无 session store |
| 任务 | 长任务无队列；Temporal 入队是 scaffold；`temporalio` 未安装 | `scheduled/service.py:541` |
| 迁移 | `migrations/` 已废弃，DDL 内联在 Python，无版本管理 | `migrations/README.md` |
| 运维 | 单机 compose；无 K8s/Helm；无备份/PITR/多环境/灰度 | 仓库无 k8s/helm |
| 安全 | 无密钥托管/轮换；提示注入/SSRF 未系统加固；无渗透测试 | `.env` + `security/` |
| 合规 | 审批只是策略类，事件“日志占位”；无审计查询/留存策略 | `security/approval.py` |
| 产品 | Billing 无 API/UI；调度无入口；Risk 归因写死；ShadowAccount 仅文案 | `api/server.py` 无 billing 路由；`Risk.tsx:229-240` |
| 发布 | 版本双源（pyproject 0.3.0 / `__version__` 0.2.0）；无 feature flag | `src/hero_quant/__init__.py:7` |

---

## 3. 目标架构

```
Client (Web)
   │  TLS / WAF / Ingress
   ▼
Auth (OIDC) ──► Tenant Context (contextvar) ──► RBAC
   │
   ▼
API (FastAPI) ── SSE Gateway ──► Agent Runtime (Loop/Graph)
   │                                │
   │                                ├─ LLM Gateway (provider failover / cost / timeout)
   │                                ├─ Tool Registry (19 tools, sandbox, idempotency)
   │                                ├─ Memory/RAG (FTS5 + pgvector + rerank)
   │                                └─ Grounding / Trace
   ▼
Data Plane: PG (checkpoint/billing/sessions/audit) · Redis (cache/ratelimit/ticket)
            Object Storage (artifacts/traces/snapshots) · PIT Store (Parquet/DuckDB)
   │
   ▼
Observability: OTel → Collector → Metrics/Logs/Traces → Grafana/Alerting
Background: Worker (long runs) · Scheduler (Temporal) · Migration Job
```

关键原则：
1. **tenant 是硬边界**：所有数据访问必须带 `tenant_id`，缺失即 fail-closed。
2. **数据可复现**：回测结果必须绑定 `snapshot_id + code_commit + config_hash`。
3. **降级必须显式**：沿用现有 `degraded + reason` 规范，前端横幅提示。
4. **生产变更走 staging**：禁止直接改生产；migrations 走版本化 job。

---

## 4. 分期与验收门

| 期 | 目标 | 包含工作流 | 验收门 |
|---|---|---|---|
| **Phase 1** | 内部团队可登录使用，Chat + Research/回测在云上稳定跑、可评测、可审计、可恢复 | W1 身份、W2 模型、W3 数据、W4 会话/迁移、W5 可观测、W6 安全基线、W7 部署、W8 合规底线、W10 发布 | **G1** |
| **Phase 2** | 全功能可用：Billing/调度/Risk 真数据、长任务队列、审批、RAG 生产化、渗透测试 | W4 队列、W9 产品面、W6 审批/渗透、W3 RAG、W2 提示词版本 | **G2** |
| **Phase 3** | 规模化与运营：HA/DR、多环境灰度、成本配额、隐私删除、运营后台 | W7 规模化、W5 SLO、W6 隐私、W9 运营、W10 发布工程 | **G3** |

### G1（Phase 1 完成定义）

1. 未登录访问 API 返回 401；tenant A 无法读写 tenant B 的会话/checkpoint/billing/memory（自动化测试覆盖）。
2. 真模型 E2E 跑通：50 条 golden set 产出基线（成功率/工具正确率/grounding 精确率/p95/单任务成本），CI 有回归门。
3. 回测可复现：同一 `snapshot_id + commit + config_hash` 重跑结果 hash 一致；默认窗口 ≥60 根。
4. 云上从零部署成功；staging→prod 发布/回滚演练通过；PG 备份恢复演练通过。
5. 仪表盘与告警可用：LLM 错误率/延迟/成本、工具失败、队列积压、PG/Redis 健康；告警测试触发。
6. 审计日志可按 tenant/user/run 查询；kill switch 可一键停用某租户/模型/工具。
7. `ruff + pytest（含 PG 真跑）+ e2e + eval` 全绿；版本单一来源。

### G2（Phase 2 完成定义）

- Billing 因子市场有 API+UI 且 tenant 隔离；调度有 Temporal worker+手动触发+运行历史；Risk 归因/ShadowAccount 真数据。
- 长任务可断点续跑、工具调用幂等、失败进 DLQ。
- 审批流可配置、可查询、不可篡改；渗透测试 P0 清零。
- RAG：pgvector + rerank 启用，retrieval eval 有 recall@k 基线。

### G3（Phase 3 完成定义）

- 多可用区/HA、压测与混沌演练报告；RPO/RTO 达标。
- 租户级成本/配额/告警；模型路由与缓存命中率可观测。
- 数据保留/删除/加密合规；运营后台与反馈闭环上线。

---

## 5. 十条工作流总览

| 编号 | 工作流 | 目标 | 主要任务 | 期 |
|---|---|---|---|---|
| W1 | 身份与租户 | 登录、租户隔离、RBAC、配额 | PR-101~105 | P1 |
| W2 | 模型网关与评测 | 真模型、failover、golden set、成本 | PR-111~115 | P1/P2 |
| W3 | 数据与回测可复现 | 快照、PIT、质量门、可复现 | PR-121~125 | P1/P2 |
| W4 | 持久化、会话与任务 | 会话、幂等、队列、迁移 | PR-131~134 / PR-201~203 | P1/P2 |
| W5 | 可观测与 SLO | 指标、看板、告警、SLO | PR-141~144 | P1/P3 |
| W6 | 安全与隐私 | 密钥、注入、SSRF、渗透、隐私 | PR-151~153 / PR-221~222 | P1/P2/P3 |
| W7 | 部署与灾备 | K8s、托管中间件、CI/CD、备份 | PR-161~165 | P1/P3 |
| W8 | 合规与审计 | 审计、审批、合规文本、kill switch | PR-171~173 / PR-241 | P1/P2 |
| W9 | 产品面 | Billing、调度、Risk、Admin、前端生产化 | PR-211~215 | P2 |
| W10 | 发布工程 | 版本单源、契约测试、feature flag | PR-181~182 / PR-341~342 | P1/P3 |

---

## 6. 任务索引

- 总清单（67 项，含依赖/规模/验收摘要）：[`backlog.md`](backlog.md)
- 第一期详细执行书：[`phase-1.md`](phase-1.md)
- 第二期详细执行书：[`phase-2.md`](phase-2.md)
- 第三期详细执行书：[`phase-3.md`](phase-3.md)

规模口径：S（1 个 PR 内）、M（1 个工作流内多 PR）、L（跨工作流/需 ADR）。

---

## 7. 执行规则

1. **一 PR 一件事**：沿用 `AGENTS.md` §1/§9；`migrations/` 需先经 §8 的治理项（先立 ADR）解冻。
2. **白名单**：每个任务在 `backlog.md` 标注允许改动的目录；越界停手回报。
3. **提交前缀**：沿用 A/B/C/D/E；生产化新线用 `P:`（Platform），正文注明工作流编号。
4. **门禁**：`ruff check src` + `pytest --cov-fail-under=50`（PG 真跑）+ 前端 build/test + `npx playwright test`；涉及模型的变更追加 eval。
5. **降级规范**：任何降级必须 `degraded=true + reason`，前端横幅；禁止静默回退。
6. **文档联动**：行为变更同步 `README` / `.env.example` / `docs/`；ADR 落 `docs/adr/`。
7. **生产变更**：只经 CI/CD 到 staging，验证后灰度到 prod；禁止手工改生产库。

---

## 8. 需要解冻/新增的治理项（需你批准）

| 项 | 现状 | 申请 |
|---|---|---|
| `migrations/` 冻结 | AGENTS §1 规定冻结；实际 DDL 内联 Python | 批准新增 `db/migrations/`（Alembic 或版本化 SQL）并把现有内联 DDL 收编；`migrations/` 旧目录保持只读 |
| 新依赖 | 无 `temporalio` / `alembic` / `authlib` | Phase 1 批准新增 `authlib`、`alembic`；Phase 2 批准 `temporalio` |
| 新目录 | 无 `auth/`、`db/`、`deploy/` | 批准新增 `src/hero_quant/auth/`、`db/`、`deploy/helm/` |
| Billing 范围 | 无 API/UI | 按 D1 保留为内部因子市场；Phase 2 补 API+UI |

---

## 9. 风险登记

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 数据源授权不清（akshare/tushare/yahoo） | 对外分发/商用法律风险 | 内部研究限定；对外前采购合规源；文档写明 |
| R2 | LLM 成本/延迟不可控 | 预算超支、体验差 | 租户预算、模型路由、缓存、告警 |
| R3 | 租户越权 | 数据泄露 | tenant 硬边界 + RLS + 隔离测试 + 审计 |
| R4 | 提示注入/工具误用 | 数据外泄/错误结论 | 信任边界、工具白名单、审批、红队 |
| R5 | schema drift | 线上事故 | 版本化迁移 + CI drift check |
| R6 | 供应商故障 | 服务中断 | 多 provider/多数据源 failover + 熔断 |
| R7 | 范围蔓延 | 交付失控 | 按期冻结范围；新需求进 Phase 3 backlog |
| R8 | 单人依赖 | 运维风险 | runbook + on-call + 交接文档 |

---

## 10. 完成定义（每个任务的 DoD）

代码 + 单测/集成测试 + 文档 + runbook（如涉及运维）+ 指标/日志 + 回滚方案 + 安全评审（如涉及权限/数据）。缺一项不算完成。
