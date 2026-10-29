# 本机开发执行计划（单机 · 当前 4C/8G 机器）

> 决策依据：D3 单机 Docker Compose、D8 本机优先开发。
> 机器事实：4 vCPU / 7.8GB RAM / 49GB 磁盘（清理后约 3.3GB 空闲）/ 2GB swap 已用满；21 个其他项目容器在跑；`llm-pg`、`llm-redis` 已存在。
> 原则：**本机只做开发/预发，不承载生产**；先完成不依赖外部账号的代码与测试，把需要 key/域名/生产机的任务单独标出。

---

## 1. 本机能力边界

| 能做 | 不能做（等生产机/外部输入） |
|---|---|
| 代码修改、单测、集成测试（连 llm-pg/llm-redis） | 生产单机部署与公网 TLS |
| 前端构建/单测/Playwright e2e | Keycloak 真登录联调（可先用 mock JWKS） |
| 真模型 E2E（有 key 即可） | Temporal 真 worker（先写代码+mock） |
| 小样本 PIT/快照/可复现 | 多年全量 PIT（磁盘不够） |
| Compose/Caddy/systemd/部署脚本编写与 `config` 校验 | 备份恢复演练（需生产机/备份盘） |
| 指标/看板/告警规则编写 | 压测/混沌/HA/DR |
| 安全用例、SBOM、依赖扫描 | 专业渗透测试 |

**Phase 1 任务本机完成度**：36 项中约 **31 项**可完成；PR-163 的实机部署验证、PR-164 恢复演练、PR-165 蓝绿切换、PR-162 的 TLS 实机验证需生产机（脚本可先写）。Phase 2 约 12/17；Phase 3 约 4/14。

**G1 七条中，本机可签 5 条**（身份、评测、可复现、审计/kill switch、门禁）；部署与备份恢复两条必须等生产机。因此本机阶段结束是“**代码就绪、待生产验证**”，不是 G1 通过。

---

## 2. 本地开发环境规范

- Python：仓库 `.venv`；worktree 内跑测试用 `PYTHONPATH=$PWD/src`。
- PG/Redis：用现有 `llm-pg` / `llm-redis`（`100.114.54.26`）；测试需显式导出 DSN，conftest 默认中和 `.env`。
- Keycloak：**先不启动**，代码按标准 OIDC 写，用本地 JWKS fixture/mock 测；真联调放生产机。
- Temporal：Phase 2 再引依赖；本机只写接口与 mock 测试。
- 前端：`npm run test -- --run`、`npm run build`；e2e 用系统 Chrome。
- Docker：只跑小型 dev compose；**不要同时起 Prometheus/Grafana/Keycloak/Temporal**（8G 不够）。
- 磁盘红线：保持 ≥2GB 空闲；只 `docker compose build` 必要镜像；构建前先 `docker builder prune -f`。

---

## 3. 执行批次（按依赖排序）

### L0 · 环境止血（无外部输入）

| 动作 | 任务 | 退出标准 |
|---|---|---|
| 清理缓存/日志，保持 ≥2GB | — | `df` 达标 |
| 跑通基线：`ruff` + `pytest` + 前端单测 + e2e | — | 全绿；记录数字 |
| 版本单源 | PR-181 | `__version__ == pyproject.version` |
| 测试基线固化（PG 真跑开关） | — | 文档记录如何导出 DSN |

### L1 · 身份与租户（代码 + mock IdP）

| 任务 | 本机做法 | 退出标准 |
|---|---|---|
| PR-101 | OIDC 校验模块 + JWKS fixture；Keycloak 配置写成 compose 服务定义，不启动 | 无 token 401、错签名 401、正确 token 通过 |
| PR-102 | tenant contextvar + 数据访问 `require_tenant` | 跨租户读写全部拒绝 |
| PR-103 | viewer/analyst/admin 依赖 + 前端路由守卫 | 角色矩阵测试 |
| PR-104 | API+DB 隔离用例 | 越权 100% 拒绝并审计 |
| PR-105 | Redis 令牌桶 + tenant 预算 | 超限 429 |

### L2 · 会话/迁移/审计（连 llm-pg）

| 任务 | 本机做法 | 退出标准 |
|---|---|---|
| PR-133 | 版本化迁移（Alembic 或 SQL + schema_version），baseline 现有 DDL | 空库/旧库 upgrade 成功；CI drift |
| PR-131 | sessions/messages/runs/tool_calls 表 + RLS | 会话可查、tenant 隔离 |
| PR-132 | 工具幂等键 + dedup 接线 | 重复调用只执行一次 |
| PR-134 | sessions API + SSE 重连 | 重启后可续跑 |
| PR-171 | audit_log + 查询 API | 按 run_id 查全链路 |
| PR-173 | kill switch（租户/模型/工具） | 一键停用 + 明确 reason |

### L3 · 模型与评测（代码先行，真跑等 key）

| 任务 | 本机做法 | 退出标准 |
|---|---|---|
| PR-111 | provider 列表/failover/健康检查 + mock provider | 主 provider 失败切备 |
| PR-112 | `scripts/eval_real.py`；无 key 时 skip | 有 key 产出真实结果；无 key 不伪造 |
| PR-113 | 50 条 golden set 骨架 + 标注模板 | 覆盖工具/回测/grounding/拒答 |
| PR-114 | 指标计算 + CI job（先 non-blocking） | 成功率/工具准确率/p95/成本可算 |
| PR-115 | tokens/cost 指标 + trace 字段 | 可按 run 查成本 |

> 阻塞：真实 50 条结果需要你提供 LLM key；golden set 的期望答案需要领域专家确认。

### L4 · 数据可复现（小样本）

| 任务 | 本机做法 | 退出标准 |
|---|---|---|
| PR-121 | `data_snapshots` 表 + manifest | 每次取数有 snapshot_id+hash |
| PR-122 | PIT 小样本（6–12 个月日线，Parquet+DuckDB） | as-of 查询正确；全量留生产机 |
| PR-123 | 质量门（缺口/异常/跨源/日历） | 坏数据阻断 |
| PR-124 | config_hash + 结果 hash | 同三元组重跑一致 |
| PR-125 | 窗口参数化 ≥60 | 默认真行情 sharpe 可用 |

### L5 · 单机部署骨架（脚本先写，实机验证后置）

| 任务 | 本机做法 | 退出标准 |
|---|---|---|
| PR-161 | `deploy/compose/docker-compose.prod.yml` + volumes/healthcheck/limits | `docker compose config` 通过 |
| PR-162 | Caddyfile + systemd unit + 防火墙脚本 | 静态检查通过；TLS 实机后置 |
| PR-163 | GitHub Actions build→push→SSH deploy→health→rollback | 流水线脚本可 dry-run |
| PR-164 | 备份脚本 + 恢复脚本 | 本地对 llm-pg 演练一次 |
| PR-165 | dev/staging/prod 配置模板 + 蓝绿切换脚本 | 模板齐全；实机切换后置 |

### L6 · 前端与产品面（可并行）

- 登录页/路由守卫、错误态、`metrics.sharpe=null` 显示 “—”、Risk 归因接真数据（依赖 L4）。
- Billing/调度页面在 Phase 2；本机可先做 API mock + 组件测试。

---

## 4. 文件归属（避免并行冲突）

| 工作流 | 主目录/文件 | 共享文件（必须串行） |
|---|---|---|
| W1 身份 | `src/hero_quant/auth/**`、`tests/test_auth_*.py` | `api/server.py`（中间件挂载）、`api/middleware.py` |
| W2 模型 | `src/hero_quant/llm/**`、`evals/**`、`scripts/eval_real.py` | `api/server.py`（LLM 初始化段） |
| W3 数据 | `src/hero_quant/data/**`、`backtest/**`、`db/migrations/**` | `api/server.py`（回测产物段） |
| W4 会话 | `src/hero_quant/session/**`、`db/migrations/**` | `api/server.py`（sessions 路由） |
| W5 观测 | `src/hero_quant/telemetry/**`、`metrics.py`、`deploy/monitoring/**` | 无 |
| W6 安全 | `src/hero_quant/security/**`、`auth` 交叉 | `api/middleware.py` |
| W7 部署 | `deploy/**`、`scripts/deploy/**`、`.github/workflows/deploy.yml` | `.env.example` |
| W8 审计 | `src/hero_quant/audit/**`、`db/migrations/**` | `api/server.py`（审计中间件） |
| W9 产品 | `frontend/**`、`api/billing.py`、`api/schedules.py` | `api/server.py`（include_router） |
| W10 发布 | `pyproject.toml`、`tests/test_bootstrap.py`、`docs/**` | 无 |

规则：同一时间只有一个工作流改 `api/server.py`；跨工作流依赖先合入再改。

---

## 5. 本机不做的任务（等生产机/外部）

| 任务 | 原因 |
|---|---|
| PR-162 TLS 实机验证 | 需要域名/公网主机 |
| PR-163 实际部署 | 需要目标机与 registry |
| PR-164 恢复演练（生产级） | 需要独立备份盘/对象存储；本地只能小演练 |
| PR-165 蓝绿实机切换 | 需要生产机 |
| PR-201/213 Temporal 真 worker | RAM/依赖不足；先写代码+mock |
| PR-222 渗透测试 | 需专业供应商 |
| PR-301~304 HA/压测/DR | 单机单盘无意义 |
| PR-322 数据采购 | 需商务/法务 |

---

## 6. 本机执行顺序（建议）

```
L0 环境止血
  → L2 迁移框架（PR-133，独立）
  → L1 身份/租户（PR-101~105）
  → L2 会话/幂等/审计（PR-131/132/134/171/173）
  → L3 模型网关/评测（PR-111/113/114/115；PR-112 等 key）
  → L4 快照/质量/可复现（PR-121/123/124/125；PR-122 小样本）
  → L5 部署骨架（PR-161~165 脚本）
  → L6 前端/产品面
```

并行建议：
- L1 与 L2 迁移框架可并行（文件不重叠）。
- L3 与 L4 可并行（llm vs data）。
- L5 可在 L1~L4 期间由另一人并行写脚本。
- `api/server.py` 的改动排队，不并行。

---

## 7. 每批的完成定义

- `ruff check src` 全过。
- `pytest` 相关用例全绿（含 PG 真跑，若能连）。
- 新增/修改行为有测试；降级有 `degraded + reason`。
- 文档/`README`/`.env.example` 同步。
- 提交带 `P:` 前缀 + 任务号；不 push。
- 本机磁盘 ≥2GB；测试后清理构建缓存。

---

## 8. 任务卡模板（每个任务开工前填，避免跑偏）

```text
任务号：PR-xxx
工作流：W1~W10
目标（一句话）：
非目标（明确不做）：
允许改的文件（白名单）：
依赖/前置：
实施步骤：
  1)
  2)
验收命令与阈值：
回滚方案：
文档同步（README/.env.example/docs）：
规模：S/M/L
```

## 9. 本机升级触发条件

出现以下任一情况，停止在本机加码，升级到 16C/32G/500G 或购置独立生产机：

- 磁盘空闲 <5GB，清理后仍无法完成一次构建。
- 需要同时跑 Keycloak + 监控 + Temporal + PG/Redis 做联调。
- 需要 10 人以上并发/压测。
- 需要做生产部署、备份恢复演练、蓝绿切换。
- 内存频繁触发 swap，测试/构建被 OOM 杀掉。

## 10. 计划变更规则

- 范围/架构变更：先更新本目录文档 + ADR，再动代码。
- 新任务：进 `backlog.md`，标期/优先级/依赖/规模；不在聊天里口头加需求。
- 任务完成：更新 `backlog.md` 状态，并在 `docs/` 留验收输出。

