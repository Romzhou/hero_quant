# Phase 2 · 功能完整（全模块可用）

> 前置：G1 通过。目标：把“有实现无入口/半截”的功能补齐，Billing/调度/Risk/RAG 全部可用。
> 验收门：`README.md` §4 的 **G2**。

---

## 1. W4 长任务队列与断点续跑

**现状**：长任务在 API 线程内跑；无队列/重试/DLQ；Temporal 仅 scaffold（`scheduled/service.py:541`），`temporalio` 未安装。
**缺口**：任务队列、worker、取消、断点、DLQ。

### 怎么做

1. **选型（PR-201）**：优先 Temporal（与 scheduled 规划一致）；若团队无 Temporal 运维能力，用 Arq/RQ + Redis，但需自己实现重试/DLQ。
2. **任务模型**：`runs` 表增加 `status/attempt/lease_until/heartbeat_at/error`；worker 拉取→执行 AgentLoop→写结果→ack；失败按退避重试，超限进 DLQ。
3. **断点续跑（PR-202）**：复用 checkpoint（PG）；worker 崩溃后从最后 checkpoint 恢复；工具调用幂等键防重复副作用。
4. **取消与进度（PR-203）**：`POST /v1/runs/{id}/cancel`；SSE 推 `run.progress/run.done/run.failed`；前端显示取消按钮。

**验收**：kill worker 后任务能续跑；重复消息只执行一次；取消 5s 内生效；DLQ 可查询/重放。

**任务**：PR-201、PR-202、PR-203。

---

## 2. W9 产品面补齐

### 2.1 Billing 因子市场（PR-211 / PR-212）

**现状**：`billing/service.py` 已 PG + RLS + 跨进程可读；**无 HTTP 路由、无前端页面**。
**怎么做**：
- API：`GET /v1/billing/factors`（tenant 过滤/分页）、`POST /v1/billing/factors`（publish）、`POST /v1/billing/factors/{id}/purchase`、`GET /v1/billing/wallet`；全部走 RBAC + tenant。
- UI：因子列表/详情/发布/购买/我的因子；显示 `degraded` 状态；无支付，仅内部额度。
- 审计：publish/purchase 写 `audit_log`；幂等键防重复购买。

**验收**：tenant A 看不到 B 的私有因子；跨进程发布后可见；购买幂等；页面无 mock 数字。

### 2.2 调度与 Playbooks（PR-213）

**现状**：5 个 playbook + cron 解析真实；`dispatch` 算出下次触发但 Temporal 入队是 scaffold；无 API/UI。
**怎么做**：
- Temporal worker：注册 5 个 workflow/activity；cron schedule 由 Temporal 管理；失败重试/告警。
- API：`GET /v1/schedules`、`POST /v1/schedules/{name}/run`、`GET /v1/schedules/{name}/runs`。
- UI：任务列表、下次触发、手动触发、运行历史/结果。
- 审计：触发人/时间/结果入审计。

**验收**：定时真实触发一次；手动触发成功；失败可在 UI 看到原因；重启 worker 后 schedule 不丢。

### 2.3 Risk 归因与 ShadowAccount（PR-214）

**现状**：Risk 页归因 5 类数字写死（`Risk.tsx:229-240`：+1.2%/+0.8%/-0.1%/-0.4%/+0.2%）；ShadowAccount 仅文案。
**怎么做**：
- 后端：新增 `/v1/risk/attribution`，从回测产物/持仓计算 5 类归因；`/v1/risk/shadow` 返回 ShadowJournal 对账结果。
- 前端：接真实接口；无数据时显示 `--`，不得回退写死数字。
- 测试：归因和为组合收益（容差）；shadow 0 差额/差异两种态。

**验收**：页面数字随回测变化；无数据不显示假数字；e2e 覆盖。

### 2.4 管理后台（PR-215）

用户/租户/角色管理、配额、审计查询、kill switch 操作、模型/工具开关。仅 `admin` 可见。可先用内部工具页，不必做完整设计系统。

**任务**：PR-211、PR-212、PR-213、PR-214、PR-215。

---

## 3. W6 审批与渗透测试

### 3.1 审批流（PR-221）

**现状**：`security/approval.py` 只有 ask/never/auto 策略；无持久化审批单/通知/回执。
**怎么做**：
- 高风险动作（数据导出、批量回测、Billing 发布、admin 操作）创建 `approval_requests`；状态 pending/approved/rejected/expired。
- 通知企业 IM；审批人角色校验；超时自动拒绝（fail-closed）。
- 审批与执行解耦：执行前校验 approval_id 且一次性消费；全程审计。

**验收**：未审批执行被拒；审批后一次性放行；超时拒绝；记录可查。

### 3.2 渗透测试（PR-222）

范围：认证/越权/注入/SSRF/文件上传/依赖/容器。P0/P1 问题清零或书面接受；报告入 `docs/security/`。

**任务**：PR-221、PR-222。

---

## 4. W3 RAG 生产化

**现状**：FTS5 + 本地向量 + RRF + 可选 Cohere rerank；`vector_dsn=none`，`cohere` 包未安装；无 recall@k 基线。
**缺口**：pgvector 启用、分块质量、rerank、评测基线。

### 怎么做

1. **pgvector（PR-231）**：配置 `HERO_VECTOR_DSN`；DDL/索引（ivfflat/HNSW）；迁移与回填；降级到本地时打 `degraded`。
2. **分块与索引（PR-232）**：长文档按标题/段落切块 + overlap；chunk metadata（doc_id/position）；增量 reindex/删除。
3. **Rerank + 评测（PR-233）**：启用 Cohere 或本地 cross-encoder；`tests/retrieval_eval` 扩展 recall@5/MRR；基线入 CI（低频）。

**验收**：pgvector 真连；recall@5 有基线且不退化；删除文档后检索不到；降级有标记。

---

## 5. W2 提示词/模型版本与路由

1. **Prompt registry（PR-251）**：提示词版本化（id/version/hash），trace 记录实际版本；变更需评审。
2. **A/B 与路由（PR-252）**：按租户/任务类型路由 deep/quick；A/B 流量比例；指标对比（成功率/成本/延迟）。

**验收**：trace 可追溯 prompt 版本；路由可配置；A/B 报告可查。

**任务**：PR-231、PR-232、PR-233、PR-251、PR-252。

---

## 6. W3 回测现实性（PR-234）

**现状**：已有换手成本/资金预检/多资产；缺滑点、借券、停牌/涨跌停、幸存者偏差。
**怎么做**：加入滑点模型（按成交额/波动）、停牌不可交易、涨跌停无法成交、退市处理、基准对比；golden 用例覆盖。**验收**：极端行情用例结果符合预期；报告注明假设。

---

## 7. W8 审计留存与不可篡改（PR-241）

审计表改为只追加 + 链式 hash（复用 ledger 思路）；保留期可配置；导出需审批；定期归档对象存储。

**验收**：篡改可检测；保留期策略生效；导出有审批记录。

---

## 8. G2 验收清单

- [ ] Billing API/UI 可用，tenant 隔离，购买幂等。
- [ ] 调度真实触发，worker 重启不丢，失败可见。
- [ ] Risk 归因/ShadowAccount 真数据，无写死数字。
- [ ] 长任务可续跑/取消/DLQ；工具幂等。
- [ ] 审批流可用，超时拒绝；渗透测试 P0 清零。
- [ ] pgvector + rerank 启用，recall@5 有基线。
- [ ] prompt/model 版本可追溯，A/B 可配。
- [ ] 回测滑点/停牌/退市假设落地。
- [ ] 审计不可篡改，保留期生效。
