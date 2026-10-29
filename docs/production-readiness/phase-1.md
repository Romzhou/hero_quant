# Phase 1 · 上线底座（内部团队可用）

> 目标：内部团队可登录使用；Chat 与 Research/回测两条链在云上稳定运行；真模型可评测；数据可复现；故障可恢复；行为可审计。
> 非目标（放 Phase 2/3）：实盘/交易指令、对外注册与支付、Billing 用户面、Temporal 调度、RAG 深度调优、多地域 HA。
> 验收门：`README.md` §4 的 **G1**。

---

## 1. 执行顺序（两条里程碑）

| 里程碑 | 内容 | 目的 |
|---|---|---|
| **M1 止血** | 真模型跑通、运行态对齐 HEAD、版本单源、密钥退出 `.env`、golden set v1、身份 ADR | 先把“能不能真跑”变成证据，避免在沙子上盖楼 |
| **M2 底座** | W1 身份/租户、W2 评测、W3 快照/PIT、W4 会话/迁移、W5 可观测、W6 安全、W7 部署、W8 审计 | 达到 G1，可给内部团队试用 |

### M1 启动包（建议第一个迭代）

| # | 动作 | 对应任务 | 完成标志 |
|---|---|---|---|
| M1-1 | 配 `HERO_API_KEY`，跑 50 条 golden query，记录 trace/usage/成本 | PR-111、PR-112、PR-113、PR-114（先跑通再补 CI） | `evals/results_real.json` 不再 `skipped` |
| M1-2 | 重建 compose 镜像，运行态对齐 HEAD | 无（运维） | 8899 返回 `sample/sharpe_basis/warnings`，不再是旧 synthetic |
| M1-3 | 版本单源 | PR-181 | `__version__ == pyproject.version` |
| M1-4 | 密钥基线：生产密钥进 Secret Manager，`.env` 退出生产 | PR-151（先做只读接入） | 生产环境无明文密钥 |
| M1-5 | golden set v1 落库 | PR-113 | 50 条含期望工具/断言/分类 |
| M1-6 | 身份/租户 ADR + IdP 选型 | PR-101（前置） | ADR 批准，IdP 确定 |

依赖关系：M1 可与 W1 的 IdP 选型并行；W2 评测依赖 M1 的 key；W3 快照先于 W4 会话；W7 部署可与 W1~W6 并行搭骨架，最后统一联调。

---

## 2. W1 身份与租户（Phase 1 核心）

**现状**：SSE 票匿名（`api/security.py:233`），无用户表/登录/RBAC；tenant 由调用方字符串传入，checkpoint 只按 `thread_id`。
**缺口**：登录、JWT 校验、tenant 硬边界、角色、租户配额、跨租户隔离测试。

### 怎么做

1. **IdP 接入（PR-101）**
   - 选型：**自建 Keycloak（OIDC，已定 D5）**；前端走 Authorization Code + PKCE，后端只校验 JWT。
   - 后端新增 `src/hero_quant/auth/`：`oidc.py`（JWKS 缓存、issuer/audience/exp 校验）、`deps.py`（`get_current_user`、`require_role`）。
   - JWT claims 至少含 `sub/email/tenant_id/roles`；缺失 tenant 直接 401，不猜默认租户。
2. **租户上下文（PR-102）**
   - 中间件解析 Bearer → `request.state.user/tenant_id` → `contextvars.ContextVar("tenant_id")`。
   - 所有数据访问入口加 `require_tenant()`：checkpoint、billing、memory、trace、session、audit。
   - checkpoint `thread_id` 改为 `f"{tenant_id}:{thread_id}"` 或加 `tenant_id` 列；billing 沿用现有 RLS；memory namespace 强制前缀 tenant。
3. **RBAC（PR-103）**
   - 角色：`viewer`（只读研究页）、`analyst`（问答/回测/导出）、`admin`（用户/租户/审计/kill switch）。
   - 端点依赖：query/stream/backtest 需 `analyst`；billing/admin 需 `admin`；只读产物需 `viewer`。
4. **配额（PR-105）**
   - Redis 令牌桶：每租户 QPS + 每日 token/成本上限；复用 `api/rate_limiter.py`，键加 tenant。
   - 超限 429 + `Retry-After`；成本上限接 `BudgetBreaker` 的 tenant 维度。
5. **隔离测试（PR-104）**
   - 用例：未登录 401；viewer 调 query 403；tenant A 读 B 的 session/checkpoint/billing/memory 404；跨租户导出被拒。
   - 数据库层：billing RLS 已有；新增 session/audit 表的 RLS 或强制 `WHERE tenant_id=`。

### 验收

- 登录 → 拿 JWT → 调 `/v1/query` 成功；无 token 401；错误签名 401。
- 跨租户读写全部拒绝，且有审计记录。
- 角色矩阵测试通过；配额 429 可复现。

**任务**：PR-101、PR-102、PR-103、PR-104、PR-105。

---

## 3. W2 模型网关与评测

**现状**：`LLMFactory` 可建 OpenAI/DeepSeek 客户端，`LLMClient` 有超时/重试；本机无 key，只跑过 `_FakeLLM`；`evals/results_real.json` 为 `skipped/no_llm_backend`。
**缺口**：真模型实证、provider failover、golden set、评测指标、成本归集。

### 怎么做

1. **Provider 网关（PR-111）**
   - `LLMFactory` 增加 provider 列表与顺序回退：primary → secondary；每个 provider 独立 timeout/retry/熔断。
   - 配置从 Settings 读（`HERO_LLM_PROVIDER`、`HERO_LLM_FALLBACK`、base_url、model）；密钥从密钥托管读取（见 W6），禁止进日志。
   - 启动时 health check（1 次 ping），失败标 degraded，不阻塞 API 启动。
2. **真模型 E2E（PR-112）**
   - 脚本 `scripts/eval_real.py`：读 golden set，逐条跑 `/v1/query`（或直接 AgentLoop），记录 trace、usage、成本、耗时。
   - 产物 `evals/results_real.json` 必须是真实结果；无 key 时明确 `skipped`，不得伪造通过率。
3. **Golden set v1（PR-113）**
   - 50 条，覆盖：单工具查询、多工具编排、行情/回测、grounding 拒答、中文金融术语、越权请求、空数据降级。
   - 每条含 `query`、`expected_tools`、`expected_contains/not_contains`、`max_latency_ms`、`category`。
4. **评测 runner 与 CI（PR-114）**
   - 指标：任务成功率、工具选择准确率、grounding 精确率/召回率、p50/p95 延迟、单任务成本。
   - CI：先 nightly 非阻断，稳定后改为 blocking；阈值以首轮基线 + 允许回退幅度设定。
5. **成本与 trace（PR-115）**
   - 每次调用记录 `provider/model/prompt_version/input_tokens/output_tokens/cost`；trace 可按 run 查。
   - 指标：`llm_tokens_total`、`llm_cost_total`、`llm_latency_seconds`（tenant/model 标签）。

### 验收

- 有 key 环境跑通 50 条，产出真实基线；无 key 时如实 skip。
- 人为让 primary provider 失败，能自动切 secondary 并标 degraded。
- trace 能查到单次调用的 tokens/成本/模型。

**任务**：PR-111、PR-112、PR-113、PR-114、PR-115。

---

## 4. W3 数据与回测可复现

**现状**：真行情可取，但 `_BACKTEST_BARS_TARGET=20`；无快照/PIT 库；回测结果不绑定数据版本。
**缺口**：数据快照、PIT 存储、质量门、结果可复现、回测现实性。

### 怎么做

1. **快照清单（PR-121）**
   - 表 `data_snapshots`：`snapshot_id`、vendor、symbols、start/end、adjust、fetched_at、row_count、sha256、storage_uri。
   - 每次取数写 manifest；回测只读快照，不直接读 live。
2. **PIT 存储（PR-122）**
   - 日线/复权因子落 Parquet（按 trade_date 分区）+ DuckDB 查询；`as_of` 查询只返回该时点可见数据。
   - 交易日历、停牌、复权、退市标记；先做日线，基本面/新闻放 Phase 2。
3. **数据质量门（PR-123）**
   - 完整性（缺口/重复）、异常值（涨跌幅/价格<=0）、跨源 1%、日历一致性；失败 fail-closed + 告警。
4. **可复现（PR-124）**
   - 回测结果写 `snapshot_id + code_commit + config_hash`；同三元组重跑结果 hash 一致（浮点容差）。
   - 产物 CSV/metrics/tearsheet 存对象存储，路径含 run_id。
5. **窗口与披露（PR-125）**
   - `_BACKTEST_BARS_TARGET` 默认 ≥250（或显式 `bars` 参数）；保留 `<60 根 sharpe=null` 的披露规则。

### 验收

- 同一 snapshot 重跑两次，metrics/positions hash 一致。
- 篡改快照或换 commit，hash 必变。
- 数据缺口/异常能阻断回测并给出 reason。

**任务**：PR-121、PR-122、PR-123、PR-124、PR-125。

---

## 5. W4 持久化、会话与迁移

**现状**：checkpoint/billing 已 PG；无会话表；工具调用无幂等；`migrations/` 废弃、DDL 内联。
**缺口**：会话持久化、工具幂等、版本化迁移、断点恢复。

### 怎么做

1. **会话 schema（PR-131）**
   - 表：`sessions(id, tenant_id, user_id, title, created_at, updated_at)`、`messages(id, session_id, role, content, trace_id, created_at)`、`runs(id, session_id, status, reason, cost, started_at, ended_at)`、`tool_calls(id, run_id, tool, args_hash, status, result_ref)`。
   - 所有表带 `tenant_id`；RLS 或强制过滤。
2. **工具幂等（PR-132）**
   - `idempotency_key = sha256(run_id + step + tool + canonical_args)`；复用 `governance/dedup.py`；重复调用返回首次结果，不重复副作用。
3. **迁移框架（PR-133）**
   - 新增 `db/migrations/`（Alembic 或版本化 SQL + `schema_version` 表）；把现有内联 DDL 收编为 baseline；CI 跑 `upgrade head` + drift check。
   - 需你批准 AGENTS §1 的 migrations 解冻（见 README §8）。
4. **会话 API（PR-134）**
   - `GET /v1/sessions`、`GET /v1/sessions/{id}`、`POST /v1/sessions/{id}/resume`；SSE 支持 `Last-Event-ID` 重连。

### 验收

- 重启 API 后会话可查、可续跑。
- 同一工具调用重复执行只产生一次副作用。
- 空库 `upgrade head` 成功；旧库升级成功；CI 无 drift。

**任务**：PR-131、PR-132、PR-133、PR-134。

---

## 6. W5 可观测与 SLO（最小集）

**现状**：Prometheus/trace/structlog 有；缺 LLM 维度指标、看板、告警、SLO。
**缺口**：指标命名与标签、Grafana 看板、告警规则、SLO/错误预算。

### 怎么做

1. **指标（PR-141）**：`llm_tokens_total`、`llm_cost_total`、`llm_latency_seconds`、`tool_calls_total{tool,status}`、`grounding_failures_total`、`runs_active`、`queue_depth`；标签 `tenant/model`。
2. **看板（PR-142）**：Grafana JSON 入仓：RED（请求/错误/延迟）+ LLM 成本 + 工具失败 + 队列。
3. **告警（PR-143）**：错误率 >5%、p95 超阈值、日成本超预算 80%、队列积压、PG/Redis 不可用、熔断 OPEN；通知到企业 IM。
4. **SLO（PR-144）**：可用性 99.5%、p95 问答 <30s、回测成功率 >99%、单任务成本上限；错误预算与升级策略写入 runbook。

### 验收

- 压测/注入故障时，看板与告警可见；告警测试真实触发一次。
- SLO 文档有指标定义、计算口径、责任人。

**任务**：PR-141、PR-142、PR-143、PR-144。

---

## 7. W6 安全基线

**现状**：沙箱/脱敏/AST guard/供应链硬化有；密钥在 `.env`；提示注入/SSRF 未系统加固。
**缺口**：密钥托管与轮换、工具输出信任边界、出网白名单、SBOM。

### 怎么做

1. **密钥托管（PR-151）**：生产用 Vault/KMS/云 Secret Manager；启动注入环境变量；禁止 `.env` 进镜像；密钥轮换流程与审计。
2. **提示注入与 SSRF（PR-152）**：
   - 工具输出视为不可信：分隔/转义、指令层级、禁止工具输出直接改变系统提示。
   - 出网：工具 HTTP 请求走 allowlist（域名/IP），拒绝 metadata/内网段；DNS 重绑定防护。
3. **安全测试（PR-153）**：注入用例集、SSRF 用例、越权用例进 CI；依赖漏洞扫描；生成 SBOM。

### 验收

- 生产环境无明文密钥；轮换后服务无中断。
- 注入/SSRF 用例全部拦截；CI 有安全 job。

**任务**：PR-151、PR-152、PR-153。

---

## 8. W7 单机部署与灾备

**现状**：只有开发用 `docker-compose.yml`（hero-quant + otel）；无反代/TLS、无生产 compose、无 systemd、无备份/PITR、无多环境。
**缺口**：单机生产编排、反向代理、CI/CD、备份恢复、staging/prod 切换。

### 怎么做

1. **生产 Compose（PR-161）**
   - 新增 `deploy/compose/docker-compose.prod.yml`：api、frontend（静态/Caddy）、keycloak、postgres、redis、otel-collector、prometheus/grafana（可选）。
   - 数据卷独立（PG/Redis/产物/备份）；`restart: unless-stopped`；healthcheck；`deploy.resources.limits`；仅 Caddy 暴露 80/443，其余走内部网络。
2. **反代/TLS/systemd（PR-162）**
   - Caddy（或 Nginx）终止 TLS、自动证书、反代 API/前端/Keycloak；安全头、上传大小、SSE 超时。
   - `deploy/systemd/hero-quant.service` 管理 compose 生命周期；日志轮转；防火墙只开 80/443；fail2ban 可选。
3. **CI/CD（PR-163）**
   - GitHub Actions：测试→构建镜像→扫描→推 registry→SSH 到目标机→`docker compose pull && up -d`→`/ready` 冒烟→失败自动回滚上一 tag。
   - 迁移作为独立 job 先执行；部署记录写入审计。
4. **备份/PITR（PR-164）**
   - PG 每日全备 + WAL 归档到独立磁盘或对象存储；Redis RDB/AOF；产物/快照卷快照。
   - 保留策略；恢复脚本；每月演练一次并记录 RPO/RTO。
5. **环境与蓝绿（PR-165）**
   - dev（本机）与 prod（独立生产机）配置/密钥隔离；同机 staging 用独立目录+端口。
   - 蓝绿：两套 app 目录/端口，Caddy 切 upstream；DB 迁移需向后兼容，回滚只切应用。

### 验收

- 生产单机从零部署成功；`/ready` 绿；TLS 正常。
- 发布失败自动回滚；蓝绿切换中断 < 10s（应用层）。
- 备份恢复演练：RPO/RTO 达标（目标值在 PR-164 内定）。
- 迁移在部署流水线中自动执行；失败不切流量。

**任务**：PR-161、PR-162、PR-163、PR-164、PR-165。

---

## 9. W8 合规与审计底线

**现状**：审批只是策略类，事件“日志占位”；无审计查询；有“不构成投资建议”文案。
**缺口**：审计日志、合规文本、kill switch。

### 怎么做

1. **审计日志（PR-171）**：表 `audit_log(id, ts, tenant, user, action, resource, run_id, model, prompt_hash, result_hash, ip)`；只追加；按 tenant/user/run 查询；保留≥1 年（按内部制度定）。
2. **合规文本（PR-172）**：产品内免责声明、隐私政策、数据来源与限制说明；登录页/页脚展示；版本化。
3. **kill switch（PR-173）**：按租户/模型/工具一键停用；Redis 标志 + 中间件/工具门检查；操作入审计。

### 验收

- 任意一次问答/回测可按 run_id 查全链路审计。
- kill switch 开启后对应能力 503 且 reason 明确。
- 合规文本可在产品内访问。

**任务**：PR-171、PR-172、PR-173。

---

## 10. W10 发布工程（Phase 1 部分）

1. **版本单源（PR-181）**：`pyproject.toml` 为唯一来源；`__version__ = importlib.metadata.version("hero_quant")`；删除硬编码 0.2.0；CI 校验一致。
2. **契约测试（PR-182）**：OpenAPI schema 快照；前端类型/接口契约测试；breaking change 需版本号。

**验收**：`python -c "import hero_quant; print(hero_quant.__version__)"` 与 pyproject 一致；OpenAPI diff 在 CI 可见。

---

## 11. G1 验收清单（逐条打勾才能进 Phase 2）

- [ ] 未登录 401；角色矩阵通过；跨租户读写全部拒绝并有审计。
- [ ] 真模型 50 条 golden set 有基线；CI eval 可回归；无 key 时如实 skip。
- [ ] 回测同 snapshot+commit+config 重跑 hash 一致；默认窗口 ≥60。
- [ ] 云上从零部署；staging→prod 灰度/回滚；PG 恢复演练通过。
- [ ] LLM/工具/队列/PG/Redis 看板与告警可用；告警测试触发。
- [ ] 审计可按 run_id 查询；kill switch 生效。
- [ ] `ruff + pytest（PG 真跑）+ e2e + eval` 全绿；版本单源。
- [ ] runbook：部署、回滚、恢复、密钥轮换、故障升级。

## 12. 风险与回滚

| 风险 | 回滚 |
|---|---|
| 登录改造影响现有匿名调用 | 保留 feature flag：`AUTH_MODE=off|enforce`，先 staging 观察再切 enforce |
| tenant 改造导致数据读不到 | 双写/双读过渡期；thread ID 迁移脚本可逆 |
| 迁移框架 baseline 不一致 | 先在空库 + staging 副本演练；失败回滚到内联 DDL |
| 单机部署与开发互相干扰 | dev compose 与生产 compose 分离；本机只做开发，生产独立主机 |
