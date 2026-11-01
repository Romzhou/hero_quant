# 批次 15 契约（2026-11-01 · 只收口，不开新线）

> 目标：把 `status-2026-11-01-batch14` 的 `5「做一半」从 7 条压到 3 条以内。
> 不做：新 PR 线、PR-122-b、Vault/KMS、生产机、数据源采购。

## 1. 本批范围与决策

| # | 事项 | 决策 |
|---|---|---|
| 1 | 定价阀门 | `HERO_PRICING_BACKEND`：`catalog`（默认，仓内价目）/ `null`（全 unpriced）/ 其它 `ValueError`；两个记账调用点传 `allow_estimate=False`（拿不到 usage 记 absent + 字符数/耗时） |
| 2 | 现实性/成本进响应 | 回测 bundle 增 `realism` + `realism_assumptions`（首根 bar 建仓行排除；positions 缺失显式 `positions_missing`；缓存同步透传）；成本状态本就在 `llm_cost.cost` |
| 3 | DB 角色 | 迁移 `0008`：建 `hero_queue_worker`（runs/run_steps 读写）与 `hero_admin_readonly`（只读），策略用 `CREATE POLICY ... TO <role>`；**删除**批次14 的 `app.queue_worker` GUC 旁路 |
| 4 | DSN 拆分 | `HERO_QUEUE_WORKER_DSN` / `HERO_ADMIN_READONLY_DSN`（Three-place：settings + .env.example + README）；空则回落 checkpoint DSN |
| 5 | 部署单元 | `deploy/systemd/hero-quant-runs-worker.service` + `hero-quant-runs-reap.service/.timer`（每 30s 回收过期租约） |

## 2. 队列 RLS 口径（替代批次14 的 SET LOCAL 方案）

- worker 连接用 `hero_queue_worker` 角色；跨租户可见性来自 policy 的 `TO hero_queue_worker USING (true)`，
  **不是**会话开关，也不是 BYPASSRLS。
- 领取事务内仍 `SET LOCAL app.tenant_id=<该行租户>`，执行期与写结果回归普通租户 RLS。
- 旧的 `set_config('app.queue_worker','on')` 已无效（有测试 `test_guc_bypass_removed` 反证）。
- 两个角色都是 `NOLOGIN`：密码与 LOGIN 由部署方 `ALTER ROLE ... LOGIN PASSWORD` 设置，仓库不落口令。

## 3. 接口变化（对外可见）

- `GET /v1/backtest/*` 产物新增 `realism` / `realism_assumptions`（ADDITIVE）。
- `llm_cost` 不变（`cost.value/status` 早已存在）。
- 无新增 HTTP 路由。

## 4. CI 事实（本轮核实）

- `.github/workflows/test.yml` 与 `ci.yml` **都有 postgres service**，CI 里 `HERO_CHECKPOINT_DSN` 已设；
  最近一次 CI 全量：`3650 passed / 9 skipped`（90 skip 只出现在本机无 DSN 时），PG 族在 CI 里真跑。
- 因此本批**没有** P0 门禁漏洞；第 5 项（CI 加 PG service）无改动。
