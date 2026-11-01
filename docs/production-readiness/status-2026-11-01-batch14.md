# hero-quant 进度报告（2026-11-01 · 批次 14 收口）

> 生成时间：2026-11-01 21:00 前后；数据全部来自本机实跑，不是估计。
> 进度真源：`docs/production-readiness/progress-2026-10-29.md` `1~`19；契约：`docs/contracts/batch-14.md`。
> 交接：`docs/production-readiness/handoff-2026-10-31.md`。

## 1. 一句话现状

master `b1f3ce7` 已 push，后端/前端/e2e 门禁全绿；批次 14 的 7 条线（PIT / 审计链 / 队列 / 定价 / 现实性 / 密钥收尾 / 管理后台）已合入。真正卡住"生产可用"的仍是三件事：**没接线的半成品**（定价阀门、现实性、会话审计）、**没验证过的生产机**、**没有真 Vault/KMS**。

## 2. 基线（本机实测）

| 项 | 值 |
|---|---|
| HEAD | `b1f3ce7`（C: AGENTS §10），与 origin/master 同步 |
| 后端测试 | **3570 passed / 90 skipped / 0 failed**（90 skip = PG 族在无 DSN 下正常跳过） |
| PG 族 | **63 passed**（`scripts/run_pg_tests.py`） |
| 前端 | vitest **122 passed**；`npm run build` 通过 |
| e2e | **32 passed / 6 project** |
| 静态检查 | `ruff check src` 全过；`gitleaks` 全树无泄漏；`pip-audit` 无已知漏洞 |
| openapi 契约 | 42 path / 46 operation（批次 14 新增 21 条，全 ADDITIVE） |
| 磁盘 | **1.4G 可用**（98% 满，紧） |
| 依赖 | duckdb 2.0.0 / pyarrow 26.0.0，区间 `>=2.0,<2.1` / `>=26,<27` |

## 3. 本会话做了什么（批次 10~14）

| 批次 | 内容 | 关键 commit |
|---|---|---|
| 10 | PR-214 Risk 归因/Shadow 真数据（5 类归因实算、2 个新端点、前端删尽写死值）；PR-151 密钥抽象（provider + env 后端 + 轮换记账） | `18e39fc` `0622e0a` |
| 11 | PR-151 接线：LLM factory/gateway/embed/rerank 全走 SecretsProvider | `da21087` |
| 12 | PII 加密口令接线 + `pii_key_status()` 探针 | `be5a636` |
| 13 | PG 口令注入 DSN（有用户名无口令时） | `e54dcf0` |
| 14 | **本批**：依赖审批落地 + 3 个迁移 + 队列 + 审计链 + PIT + 定价层 + 回测现实性 + 密钥收尾 + 管理后台 | 11 个提交，`38976c5`…`d2998c0` |
| 收尾 | 依赖区间收窄（duckdb 收 minor）+ AGENTS §10 规则 | `169b654` `b1f3ce7` |

## 4. 批次 14 明细（做了什么）

| 线 | 交付 | 实测证据 |
|---|---|---|
| 依赖/迁移 | duckdb+pyarrow；`0005` 队列列+run_steps、`0006` 审计链哈希+链头、`0007` current_tenant 支持 `app.tenant_id` | lock 语义 diff 仅 8 条；hash dry-run 通过 |
| PR-122-a PIT | `data/pit_store.py`：Parquet 按 `trade_date` 分区 + DuckDB as-of | 真数据 600519.SH 726 行 / 726 分区 / 1,929,708 B；未来函数被挡 |
| PR-241 审计链 | `audit/chain.py` + `producers.py` + `retention.py` + `GET /v1/audit/verify` | 39+6 离线、13 真 PG（篡改/删行/截尾/并发 8 写） |
| PR-201~203 队列 | `runs/{store,steps,worker,api,guards,cli}.py`：SKIP LOCKED 领取、SET LOCAL、fencing、幂等 step、SSE | 26 单元 + 10 真 PG（SIGKILL 续跑 / 3 worker 抢 10 任务 / 僵尸写入被拒） |
| PR-112 定价层 | `pricing/` + `LLMUsage` 扩字段 + eval 门禁改次数/耗时 | 22 新增 + 129 回归 |
| PR-234 现实性 | `backtest/realism.py`（滑点/涨跌停/停牌/退市/基准） | 36 用例；`engine.py` 零改动 + 逐字段回归 |
| PR-151 收尾 | `secrets/redis.py`（DSN 注口令）+ `audit_bridge.py`（轮换入链） | 30 用例 |
| PR-215 管理后台 | `admin/` 6 端点 + `pages/admin/Admin.tsx` + Live 成本改「未定价」 | 40 离线 + 7 真 PG + 5 vitest |
| 主控接线 | 路由双前缀、`HERO_PIT_ROOT`、redis 口令、轮换 hook、tool/backtest 审计生产者、`python -m hero_quant.runs` | openapi 21→42 |

## 5. 做一半的清单（每条都有实测依据，不是猜）

| # | 项 | 现状 | 差什么 |
|---|---|---|---|
| 1 | **PR-112 定价** | 阀门默认 `null` provider，成本记 `unpriced` | gateway 仍 `allow_estimate=True`，免费源"记 absent 不伪造 token"只在单测生效；没有定价 env 开关 |
| 2 | **PR-234 现实性** | `realism_report`/`buy_and_hold` 库函数 + 测试 | 没接进任何 API 响应；`engine.run` 也没加参数 |
| 3 | **PR-241 审计** | 链覆盖 `query.*`/`tool.call`/`backtest.run`；verify 端点可用 | `session.run` 生产者未接（会话级暂由 `query.*` 覆盖）；历史行为 NULL；`retention_plan` 只读不删 |
| 4 | **PR-201~203 队列** | 代码 + CLI 完整 | 生产未建 `hero_queue_worker` 角色（policy 未收窄 `TO`）；无 systemd/compose unit；取消只在 step 边界 |
| 5 | **PR-122 PIT** | 122-a：单标的 3 年切片跑通 | 122-b 全量（多标的多年的批处理 + 磁盘评估）；`snapshot_id` 未进 PR-124 的 `reproducibility` |
| 6 | **PR-151 密钥** | LLM/PII/PG/Redis 口令 + 轮换入链 | `security/credentials.py` 未接 provider；无真 Vault/KMS；无自动轮换/吊销；无密钥年龄度量 |
| 7 | **PR-215 管理后台** | 6 端点 + 页面可用 | 跨租户 overview/tenants 需 DB 角色 SUPERUSER/BYPASSRLS，普通角色下退回 `scope=tenant` |
| 8 | **PR-112 评测** | 定价层铺好管线 | 真实评测仍 36/50（0.72）；12 条 latency 超预算；usage 全 `estimated`、cost 无真值 |
| 9 | **PR-124 可复现** | 三元组齐全 | `data_fingerprint` 绑的是本次行情，不是 PIT snapshot |
| 10 | **PR-171 审计** | `/v1/audit` + 链A 4 action | 工具级/回测级已补，会话级未补（见 #3） |
| 11 | **PR-143 告警** | 规则/看板就绪 | Alertmanager 仍 `unconfigured` 不投递；promtool/amtool 未跑 |
| 12 | **PR-161~165 部署** | compose/systemd/备份/蓝绿脚本齐全 | 全部"生产机未验"：TLS、备份恢复、蓝绿切换没实机跑过 |

## 6. 计划文档执行到哪

- `progress-2026-10-29.md`：写到 **§19**（批次 1~14 逐批留档，含每批"如实边界"）。
- `backlog.md`（67 项）：**42/67** —— Phase 1 **35/36**（只剩 PR-122 全量）、Phase 2 **7/17**（201/202/203/214/215/234/241 完成）、Phase 3 **0/14**。
- `handoff-2026-10-31.md`：已同步到批次 14（master/门禁/下一步/坑位）。
- `docs/contracts/batch-14.md`：本批接口契约（278 行），后续批次按它实现。
- `status-and-inputs-2026-11-01.md`：面向决策的输入清单（其中 A 依赖 / B 队列 / C 磁盘 已批复并落地）。

## 7. 需要你决策 / 外部输入

| # | 事 | 影响 | 现状 |
|---|---|---|---|
| A | 生产机（8C/16G + 独立备份盘） | PR-161~165 验收、Phase 3 全部 | **未提供** |
| B | 真 Vault/KMS | PR-151 才算真托管 | **未提供**，当前 env 明文 |
| C | LLM 预算与可接受延迟口径 | PR-112 回归门、PR-252 路由 | **未定** |
| D | 数据源合规采购 | 对外发布（PR-322） | **未采购**，仅内部研究 |
| E | 队列 worker 的 DB 角色（`hero_queue_worker`，不给 BYPASSRLS） | 队列多租户隔离闭环 | 需 DBA 建角色（本批未做） |
| F | 管理后台跨租户视角（SUPERUSER 或 BYPASSRLS） | `/v1/admin/tenants` 是否可用 | 需运维定 |

## 8. 风险与过程事故（如实记）

1. **提交消息串台（已修）**：批次 14 首次提交时用了 `/tmp/m2.txt`/`m3.txt`（10-28 遗留的旧草稿），两条 commit 消息与内容不符。发现后 `git reset --mixed` 退回、核对文件完好、用独立文件名重提。**内容零损失**，但暴露了"复用 `/tmp` 固定文件名"的坏习惯。
2. **自伤一次（已修）**：我把 `bundle = _compute_backtest_bundle()` 包成包装函数，触发结构性测试 `test_retest271_lane_e2` 失败；改为"先算再审计"两行，测试未改。
3. **磁盘长期紧张**：1.2~1.4G 可用。PR-122-b 扩量前必须先解决（清 docker 缓存或加盘）。
4. **空闲会话已清**：10-29 遗留的 3 个 `--print` 会话（12 个进程）已 SIGTERM 全清，当前同工作树只有本会话。

## 9. 建议下一步（按性价比排序）

1. **PR-112 收尾**（半天级）：gateway 传 `allow_estimate=False` + 定价 backend env（`null`/`catalog` 两值 + 三处联动）→ 免费源真正落 `absent`、成本显示"未定价"。这是唯一"管线已铺好、只差一根线"的活。
2. **把 `realism_report` / `cost.status` 接进响应**：回测响应挂 `realism` 键、查询响应挂 `cost.status`，前端已有"未定价"渲染能力。
3. **队列上线准备**：systemd unit + `hero_queue_worker` 角色脚本（写进 `docs/SECURITY-SECRETS.md` §7 已列）、把 `reap_expired` 挂成定时任务。
4. **PR-122-b 扩量**：先做磁盘评估（100M/标的/3 年的实测基线已有了），再决定全市场还是沪深300。
5. **等生产机**：到位后立刻做 PR-161~165 的 TLS / 备份恢复 / 蓝绿演练，那是 Phase 1 最后一块。
### 5.1 批次15 收口结果（2026-11-01 晚）

| 原「做一半」项 | 状态 | 证据 |
|---|---|---|
| 定价阀门（原 #1） | **已收口** | `HERO_PRICING_BACKEND`（catalog 默认 / null 关闭 / 非法 ValueError）+ 两个记账点 `allow_estimate=False`；6 例接线测试 |
| 现实性未接响应（原 #2） | **已收口** | 回测产物带 `realism`/`realism_assumptions`，真行情 60 根实测 118 笔、cost_bps 7.39、无降级；缓存同步透传 |
| 队列角色未建（原 #4 部分） | **已收口** | 迁移 `0008`：`hero_queue_worker` + `hero_admin_readonly`，`CREATE POLICY ... TO role`，**删除** GUC 旁路（有反证测试）；DSN 拆 `HERO_QUEUE_WORKER_DSN`/`HERO_ADMIN_READONLY_DSN` |
| 队列部署单元（原 #4 部分） | **已收口** | `deploy/systemd/hero-quant-runs-worker.service` + `runs-reap.service/.timer`（30s）；CLI 增 `--reap` |
| 管理后台跨租户（原 #7） | **已收口** | 只读角色 + TO role 只读策略，不再依赖 SUPERUSER/BYPASSRLS |

**剩余自主可收口项（3 条，达标）**：PR-241 会话级生产者（`session.run`）、PR-122-b PIT 扩量（等体积结论）、PR-112 真评测按「次数+耗时」跑一轮。
**外部依赖项（不计入）**：生产机验收、真 Vault/KMS、数据源采购、告警投递（Alertmanager）。

**CI 核实**：`test.yml`/`ci.yml` 均有 postgres service，CI 全量 `3650 passed / 9 skipped`（90 skip 只在本机无 DSN 时出现）→ RG 族在 CI 真跑，**无 P0 门禁漏洞**，批次15 第 5 项无需改动。

**体积结论（禁止外推）**：PIT 726 个 parquet 文件**每个恰好 2658 B、每个只有 1 行**；单文件 footer 1836 B（约 69%）。
即 1.93MB ≈ 726 × (1836 元数据 + 822 数据)，是**文件级开销主导**，不能用来外推 122-b 的磁盘需求；
122-b 必须换分区粒度（按 symbol×年/月，而不是每交易日一文件）再评估。
