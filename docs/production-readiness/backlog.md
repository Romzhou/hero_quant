# 生产级任务总清单（Backlog）

> 基线：`7383e4e` · 规模：S（单 PR）/ M（多 PR）/ L（跨工作流，需 ADR）
> 优先级：P0 = G1 阻断；P1 = G2 阻断；P2 = G3/优化
> 负责建议：后端 / 前端 / 平台 / 数据 / 安全
> 本机（4C/8G）可做与后置项见 [`local-plan.md`](local-plan.md)。
> **进度与验收留档**：[`progress-2026-10-29.md`](progress-2026-10-29.md)（含已合入 commit、在途、未做、已知缺陷、冻结区裁定）。

## 汇总

| 期 | 任务数 | P0 | P1 | P2 |
|---|---|---|---|---|
| Phase 1 | 36 | 28 | 8 | 0 |
| Phase 2 | 17 | 0 | 16 | 1 |
| Phase 3 | 14 | 0 | 0 | 14 |
| **合计** | **67** | **28** | **24** | **15** |

**关键路径（先做这些）**：PR-112 → PR-113 → PR-114（真模型与评测）→ PR-101/102（身份租户）→ PR-121/124（快照可复现）→ PR-161/163（云部署）→ PR-171/173（审计/kill switch）→ G1。

---

## 未接线/未生效清单（合并审查，2026-11-02）

两份审查合并去重后的事实清单。**只列能给出代码证据的条目**；原始审查里 P0-1 / P0-2 / P1-* 的文字本会话未取得，不在此编造，下一会话按原文补。

| ID | 问题 | 级别 | 证据（文件 + 函数，无行号） | 后果 |
|---|---|---|---|---|
| P0-3 / B3 | 两个同名 `MemoryStore`，装配接错 | P0 | `agent/memory/store.py::MemoryStore`（进程内 LRU 会话缓冲，参数 `max_sessions`）与 `memory/store.py::MemoryStore`（文件 + SQLite FTS5 持久化，参数 `base_path`/`namespace`）同名；`api/server.py` 启动注入调的是 `agent/memory/store.py::inject_memory_store` | 持久记忆从不落盘、不可检索；接错不报错、测试全绿 |
| P0-4 / B4 | `DedupStore` 未接线 | P0 | `governance/dedup.py::DedupStore` 在 `agent/loop.py`、`tools/registry.py` 中零引用（grep 无命中） | 队列重试会重复执行工具副作用（写文件/下单/发通知） |
| P0-5 / B1 | 会话写端点不存在 | P0 | `api/server.py` 只有 `GET /v1/sessions`、`GET /v1/sessions/{id}`、`POST /v1/sessions/{id}/resume`；`create_session` / `start_run` / `finish_run` 无任何生产调用方 | 会话无法创建；批次16 接的 `session.run` 审计永不触发 |
| P2-1 / B2 | 数据源白名单 16 个，运行时只注册 2 个 | P2 | `data/sources.py::VALID_SOURCES`（16 个）vs `tools/market_data.py` 仅 `register(TencentLoader())` 与 `register(YahooLoader())` | **批次18 已修（文档口径）**：`registry.py` docstring / `data/arch/data.md` / `COMPLIANCE.md` / `sources.py` 注释统一写明「白名单只是契约枚举，运行时仅两源、按注册顺序回退，无 16 源择优」；`tests/test_batch18_source_whitelist_doc.py` 锁定事实。恢复多源仍另开线。Yahoo 在本机全端点 403 |
| P2-2 / 同名类 | 全仓 3 组同名类（按类名严格统计） | P2 | ① `DataValidationError` ×4：`data/loaders/tencent.py`、`yahoo.py`、`akshare_loader.py`、`ccxt_loader.py`；② `ApprovalService`：`security/approval.py` 与 `interaction/approval.py`（后者继承前者别名）；③ `RunNotFound`：`runs/store.py`(RunsError) 与 `session/store.py`(SessionError)，两者都活跃使用、风险最高 | **批次18 已修**：① 四个 loader 共用 `data/loaders/errors.py::DataValidationError`；② `interaction/approval.py` 改名 `InteractionApprovalService`（兼容别名保留）；③ `session/store.py` 改名 `SessionRunNotFound`（兼容别名保留）。`tests/test_batch18_no_duplicate_class_names.py` 按类名严格扫描锁零重复（阶段0 给定的 awk 整行比对会漏掉基类不同的 ②③）。 |
| P1-10 / 记忆检索 L2 缓存跨实例串数据 | Redis L2 检索缓存按 `hero:cache:memory:search:{ns}:{query}:{backend}` 全局键控，不含 store/base_path | `memory/store.py::MemoryStore.search` / `_invalidate_redis_search_cache`；批次17 阶段2 的跨进程用例初版因共用 token 被上一用例的 L2 缓存命中而假绿，改用唯一 marker 后反证 | **批次18 已修**：新增 `MemoryStore._store_tag/_l2_prefix/_l2_key`，L2 键与失效前缀都含解析后数据根身份；`tests/test_batch18_memory_l2_isolation.py` 用 fakeredis 反证两个 store 不互相命中、失效不误删。 |
| P1-9 / 接地回显自证 | 工具结果回显进 buffer 后「任一价格命中证据即整段放行」 | P1 | `agent/loop.py::AgentLoop.run` 的接地校验逐数字 try `assert_price`，而 `_handle_result` 把工具结果回显进 buffer；批次17 阶段1 用 pad 把 bars 数值挤出回显窗口复现 | **批次18 已修**：`AgentLoop.run` 记录每次工具回显片段，step 7 从 buffer 剥离后再抽 claims，回显数字不再替模型断言自证；`tests/test_batch18_grounding_echo.py` 锁「回显真价格 + 幻觉价 → verified False」。批次17 修的是 ingest 缺失（已 REAL）。 |

> 批次18（单 agent 串行，无 git 写）：P2-2 同名类 3 组统一、P1-10 L2 缓存按 store 隔离、P1-9 接地回显剥离、P2-1 数据源文档口径，均先写测试跑红再修；新增 4 个 `tests/test_batch18_*.py`。P1-11/P1-12 仓库无定义未编造；P0-5 会话写端点按批次17 结论保持「未接」不做。
> 批次17 阶段5 独立审查：接线项 1-4 结论 REAL；`SessionStore` 进入 /v1/query 链路仍 FAKE（预期「未接」，如实标）；另发现 `ApprovalService` / `RunNotFound` 两组同名类，已并入本表 P2-2。
> 批次17 阶段1：P0-1 / P0-2 / P0-4 已接线（真实调用点见 `AgentLoop._handle_result`、`AgentLoop.run._exec_spec`、`MarketDataRegistry.get_bars`）；P0-3 阶段2处理。
> 批次17 决策：接地校验粒度（P1-9）按主控建议排期批次18；阶段3 装配用真实模块级 `app` + `TestClient`（仓库没有 `create_app()`），不引入工厂重构。
> 去重规则（已执行）：B4 = P0-4；B1 = P0-5；B3 = P0-3（采用 P0-3 表述，含同名类陷阱）；B2 = P2 第一条。
> 批次17 阶段0：`agent/memory/store.py::MemoryStore` 已改名 `SessionBuffer`，同名类只剩持久化侧；装配接线在阶段2处理。
> 归属：B1/B3/B4 同时符合下方「L1 但未接线」口径。

## 完成度口径（L1–L4，2026-11-02 起）

| 级别 | 定义 | 是否计入完成 |
|---|---|---|
| L1 | 代码就绪（模块 + 单测存在） | 否 |
| L2 | 接线可达（路由 / 工具注册 / CLI / 中间件 / CI 作业能触达） | **是** |
| L3 | 路径覆盖（有端到端或真栈集成用例覆盖主路径） | 是 |
| L4 | 生产验证（目标环境跑过并留证） | 是 |

**重算结果（取代此前"42/67"）**：**L2 = 29 / 67**（L3、L4 本表不主张，无端到端或生产证据时不下调也不上调）。
Phase 1：26/36（原 35）；Phase 2：7/17（原 7，其中队列、审计、Risk、管理后台、现实性、定价均 L2）。

## L1 但未接线（逐条证据）

> 审查项 B1（会话端点）、B3（同名 MemoryStore）、B4（DedupStore）同样属于本级，见上方「未接线/未生效清单」，此处不重复列。

| ID | 任务 | 未接线证据 |
|---|---|---|
| PR-112 | 真模型 E2E | 只有 `scripts/eval_*.py`，手动跑；无应用/CI 触达 |
| PR-121 | 数据快照清单 | `SnapshotStore` / `capture_snapshot` 在 `src/` 内除自身与 `pit_store` 外无调用方 |
| PR-123 | 数据质量门 | `assert_quality` 只被未接线的 `SnapshotStore` 使用 |
| PR-132 | 工具调用幂等 | `governance/dedup.py` 的 `DedupStore` 在 `agent/loop.py`、`tools/registry.py` 中零引用（批次17 P0 修） |
| PR-142 | Grafana 看板 | `deploy/monitoring/grafana/` 有产物，未验证 provisioning |
| PR-143 | 告警规则 | `alertmanager.yml` 在 observability profile 下，未配置真实投递 |
| PR-144 | SLO/错误预算 | `slo.yml` 与文档，未接评估/告警 |
| PR-161 | 生产 compose | `docker-compose.yml` 存在，未在目标机跑过 |
| PR-162 | 反代/TLS/systemd | unit/caddy 配置存在，未实机验证 |
| PR-163 | CI/CD 单机发布 | 发布脚本未在任何环境执行 |
| PR-164 | 备份/PITR | `hero-quant-backup.service/.timer` 存在，未跑过恢复演练 |
| PR-165 | 环境隔离与蓝绿 | 蓝绿脚本存在，未演练 |

**待复核（1 项）**：PR-181 版本单源 —— 未找到 CI 校验或统一入口脚本，暂记 L1，下一批复核。

## Phase 1（G1 上线底座）

| ID | 优先级 | 工作流 | 任务 | 交付物 | 验收摘要 | 依赖 | 规模 | 负责 |
|---|---|---|---|---|---|---|---|---|
| PR-101 | P0 | W1 身份 | OIDC/SSO 登录与 JWT 校验 | `auth/` 模块、登录页、JWKS 缓存 | 无 token 401；错误签名 401；登录可用 | — | M | 后端+前端 |
| PR-102 | P0 | W1 身份 | tenant 上下文与数据硬边界 | contextvar、tenant 列/前缀、`require_tenant` | tenant A 读不到 B 的 session/checkpoint/billing/memory | PR-101 | M | 后端 |
| PR-103 | P0 | W1 身份 | RBAC（viewer/analyst/admin） | 角色表、端点依赖、前端路由守卫 | 角色矩阵测试通过 | PR-101 | M | 后端+前端 |
| PR-104 | P0 | W1 身份 | 跨租户隔离测试 | 自动化用例（API+DB） | 越权全部拒绝且有审计 | PR-102/103 | S | 测试 |
| PR-105 | P1 | W1 身份 | 租户 QPS/成本配额 | Redis 令牌桶、BudgetBreaker tenant 维度 | 超限 429；成本上限生效 | PR-102 | M | 后端 |
| PR-111 | P0 | W2 模型 | Provider 网关与 failover | provider 列表、健康检查、熔断 | primary 故障切 secondary 并标 degraded | — | M | 后端 |
| PR-112 | P0 | W2 模型 | 真模型 E2E 跑通 | `scripts/eval_real.py`、真实 results | 50 条真实结果；无 key 如实 skip | PR-111 | S | 后端 |
| PR-113 | P0 | W2 模型 | Golden set v1（50 条） | `evals/golden/*.jsonl` | 覆盖工具/回测/grounding/拒答/中文术语 | — | M | 数据+后端 |
| PR-114 | P0 | W2 模型 | 评测 runner + CI 回归门 | 指标报告、CI job | 成功率/工具准确率/grounding/p95/成本可回归 | PR-112/113 | M | 后端+平台 |
| PR-115 | P1 | W2 模型 | 成本与 trace 归集 | tokens/cost 指标、trace 字段 | 可按 run 查模型/版本/tokens/成本 | PR-111 | S | 后端 |
| PR-121 | P0 | W3 数据 | 数据快照清单 | `data_snapshots` 表、manifest | 每次取数有 snapshot_id+hash | — | M | 数据 |
| PR-122 | P0 | W3 数据 | PIT 日线存储与 as-of 查询 | Parquet+DuckDB、交易日历 | 只返回时点可见数据 | PR-121 | L | 数据 |
| PR-123 | P0 | W3 数据 | 数据质量门 | 完整性/异常/跨源/日历检查 | 坏数据阻断并给 reason | PR-121 | M | 数据 |
| PR-124 | P0 | W3 数据 | 回测可复现 | config_hash、结果 hash、对象存储 | 同快照+commit+config 重跑一致 | PR-121/122 | M | 后端+数据 |
| PR-125 | P0 | W3 数据 | 回测窗口 ≥60 根 | 参数化 bars、披露规则保留 | 默认真行情 sharpe 可用 | — | S | 后端 |
| PR-131 | P0 | W4 会话 | 会话/消息/运行表 | `sessions/messages/runs/tool_calls` | 会话可查、tenant 隔离 | PR-102 | M | 后端 |
| PR-132 | P0 | W4 会话 | 工具调用幂等 | 幂等键 + dedup 接线 | 重复调用只执行一次 | PR-131 | M | 后端 |
| PR-133 | P0 | W4 迁移 | 版本化迁移框架 | `db/migrations/`、baseline、CI drift | 空库/旧库 upgrade 成功 | 需批准 | L | 平台 |
| PR-134 | P1 | W4 会话 | 会话查询/续跑 API | sessions API、SSE Last-Event-ID | 重启后可续跑 | PR-131 | M | 后端 |
| PR-141 | P0 | W5 观测 | LLM/工具/队列指标 | Prometheus 指标 | 指标可按 tenant/model/tool 查 | — | S | 后端 |
| PR-142 | P1 | W5 观测 | Grafana 看板 | 看板 JSON 入仓 | RED+成本+工具+队列可见 | PR-141 | S | 平台 |
| PR-143 | P0 | W5 观测 | 告警规则 | 告警配置、IM 通知 | 故障注入能触发告警 | PR-141 | M | 平台 |
| PR-144 | P1 | W5 观测 | SLO/错误预算 | SLO 文档、计算口径 | 月度报告可生成 | PR-141 | S | 平台 |
| PR-151 | P0 | W6 安全 | 密钥托管与轮换 | Vault/KMS 接入、轮换流程 | 生产无明文密钥；轮换不中断 | — | M | 安全+平台 |
| PR-152 | P0 | W6 安全 | 提示注入/SSRF 加固 | 信任边界、出网白名单 | 注入/SSRF 用例拦截 | — | M | 安全+后端 |
| PR-153 | P1 | W6 安全 | 安全测试与 SBOM | CI 安全 job、SBOM | 依赖漏洞可见；用例进 CI | PR-152 | S | 安全 |
| PR-161 | P0 | W7 部署 | 生产 compose（单机） | `deploy/compose/docker-compose.prod.yml` | staging 从零起全栈；仅反代暴露 80/443 | — | M | 平台 |
| PR-162 | P0 | W7 部署 | 反代/TLS/systemd | Caddy/Nginx、systemd unit、防火墙 | TLS 正常；开机自启；重启自愈 | PR-161 | M | 平台 |
| PR-163 | P0 | W7 部署 | CI/CD 单机发布 | build/push/SSH deploy/health/rollback | 发布失败自动回滚；迁移先执行 | PR-161 | M | 平台 |
| PR-164 | P0 | W7 部署 | 备份/PITR 与恢复 | 全备+WAL、恢复脚本、演练 | 恢复演练 RPO/RTO 达标 | PR-161 | M | 平台 |
| PR-165 | P1 | W7 部署 | 环境隔离与蓝绿 | dev/prod 配置、双目录/端口切换 | 蓝绿切换 <10s；密钥隔离 | PR-163 | M | 平台 |
| PR-171 | P0 | W8 合规 | 审计日志 | `audit_log` 表、查询 API | 按 run_id 查全链路 | PR-102 | M | 后端 |
| PR-172 | P0 | W8 合规 | 合规文本 | 免责/隐私/数据源说明 | 产品内可访问、版本化 | — | S | 产品 |
| PR-173 | P0 | W8 合规 | kill switch | 租户/模型/工具开关 | 一键停用并返回明确 reason | PR-102 | S | 后端+平台 |
| PR-181 | P0 | W10 发布 | 版本单源 | `__version__` 从元数据取 | pyproject 与运行时一致 | — | S | 后端 |
| PR-182 | P1 | W10 发布 | OpenAPI 契约测试 | schema 快照、CI diff | breaking change 可见 | — | S | 后端 |

## Phase 2（G2 功能完整）

| ID | 优先级 | 工作流 | 任务 | 交付物 | 验收摘要 | 依赖 | 规模 | 负责 |
|---|---|---|---|---|---|---|---|---|
| PR-201 | P1 | W4 队列 | 长任务队列/worker | Temporal 或 Arq + worker | 任务异步执行、重试、DLQ | PR-131 | L | 后端+平台 |
| PR-202 | P1 | W4 队列 | 断点续跑 | checkpoint 恢复、幂等 | worker 崩溃后续跑 | PR-201/132 | M | 后端 |
| PR-203 | P1 | W4 队列 | 取消与进度 | cancel API、SSE 进度 | 取消 5s 内生效 | PR-201 | S | 后端 |
| PR-211 | P1 | W9 产品 | Billing API | factors list/publish/purchase/wallet | tenant 隔离、购买幂等 | PR-102/103 | M | 后端 |
| PR-212 | P1 | W9 产品 | Billing UI | 因子列表/详情/发布/购买 | 无 mock；degraded 可见 | PR-211 | M | 前端 |
| PR-213 | P1 | W9 产品 | 调度 worker/API/UI | Temporal workflows、触发/历史 | 定时真实触发；重启不丢 | PR-201 | L | 后端+前端 |
| PR-214 | P1 | W9 产品 | Risk 归因/Shadow 真数据 | attribution/shadow API + 页面 | 数字随回测变化；无写死 | PR-124 | M | 后端+前端 |
| PR-215 | P1 | W9 产品 | 管理后台 | 用户/租户/配额/审计/开关 | admin 可日常运维 | PR-103/171 | M | 前端+后端 |
| PR-221 | P1 | W6 安全 | 审批流 | 审批单、通知、一次性消费 | 未批拒绝；超时拒绝 | PR-171/103 | M | 后端 |
| PR-222 | P1 | W6 安全 | 渗透测试 | 报告、P0 修复 | P0 清零或书面接受 | PR-101~152 | L | 安全 |
| PR-231 | P1 | W3 RAG | pgvector 启用 | DSN/DDL/索引/回填 | 真连 pgvector；降级有标记 | PR-122 | M | 数据 |
| PR-232 | P1 | W3 RAG | 分块/增量索引 | chunk pipeline、reindex/删除 | 删除后检索不到 | PR-231 | M | 数据 |
| PR-233 | P1 | W3 RAG | Rerank + recall 评测 | Cohere/cross-encoder、recall@5 | 基线可回归 | PR-231/113 | M | 数据+后端 |
| PR-234 | P1 | W3 数据 | 回测现实性 | 滑点/停牌/涨跌停/退市/基准 | 极端行情用例符合预期 | PR-124 | M | 数据 |
| PR-241 | P1 | W8 合规 | 审计不可篡改/留存 | 链式 hash、保留策略、归档 | 篡改可检测；保留生效 | PR-171 | M | 后端 |
| PR-251 | P1 | W2 模型 | Prompt registry | 版本/hash/评审 | trace 可追溯 prompt 版本 | PR-115 | M | 后端 |
| PR-252 | P2 | W2 模型 | A/B 与模型路由 | 路由配置、A/B 报告 | 成本/质量对比可查 | PR-251 | M | 后端 |

## Phase 3（G3 规模化）

| ID | 优先级 | 工作流 | 任务 | 交付物 | 验收摘要 | 依赖 | 规模 | 负责 |
|---|---|---|---|---|---|---|---|---|
| PR-301 | P2 | W7 可靠性 | 单机加固 | 数据盘/资源 limits/补丁/监控 | 磁盘/内存/进程故障演练有记录 | PR-161 | M | 平台 |
| PR-302 | P2 | W7 容量 | 压测与容量基线 | k6/Locust 脚本、报告 | 单机目标并发无雪崩 | PR-301 | M | 平台 |
| PR-303 | P2 | W7 演练 | 故障演练 | 注入脚本、演练记录 | 降级/熔断/告警/恢复正确 | PR-301 | M | 平台 |
| PR-304 | P2 | W7 升级 | 可选双机/托管/K8s | 切换/回切方案 | 仅在需要 HA 时演练达标 | PR-301 | L | 平台 |
| PR-311 | P2 | W5 成本 | 租户预算/账单 | 预算、告警、账单导出 | 超预算降级；对账一致 | PR-105/115 | M | 后端 |
| PR-312 | P2 | W5 成本 | 模型路由/缓存优化 | 路由策略、缓存、对照 | 成本下降且质量不退化 | PR-252 | M | 后端 |
| PR-313 | P2 | W5 SLO | 月度 SLO 报告 | 自动报告、错误预算 | 超预算触发发布冻结 | PR-144 | S | 平台 |
| PR-321 | P2 | W6 隐私 | 保留/删除/加密 | 删除 API、保留策略、加密 | 删除可验证；备份过期清理 | PR-171 | L | 安全+后端 |
| PR-322 | P2 | W6 合规 | 数据授权/采购 | 数据源许可清单、合同 | 对外前完成采购 | — | M | 产品+法务 |
| PR-323 | P2 | W6 安全 | 红队复测 | 报告、修复记录 | 无未接受高危 | PR-222 | M | 安全 |
| PR-331 | P2 | W9 运营 | 运营后台 | 用户/租户/配额/审计/开关 | 日常运维可自助 | PR-215 | M | 前端+后端 |
| PR-332 | P2 | W9 运营 | 反馈/评测闭环 | 反馈池、标注、回归 | 反馈到 golden set 闭环 | PR-114/331 | M | 产品+数据 |
| PR-341 | P2 | W10 发布 | Feature flags/灰度 | 开关、按租户/比例灰度 | 一键回滚；变更审计 | PR-163 | M | 平台 |
| PR-342 | P2 | W10 发布 | 发布列车/版本策略 | 发布节奏、弃用策略 | 发布/回滚清单可执行 | PR-341 | S | 平台 |

---

## 依赖关系（简图）

```
PR-101 ─► PR-102 ─► PR-103 ─► PR-104
             │         └────► PR-105
             ├────► PR-131 ─► PR-132 ─► PR-134 ─► PR-201 ─► PR-202/203
             ├────► PR-171 ─► PR-241
             └────► PR-173

PR-111 ─► PR-112 ─► PR-114
PR-113 ─┘
PR-115 ─► PR-251 ─► PR-252

PR-121 ─► PR-122 ─► PR-124 ─► PR-214/234
   └────► PR-123
PR-125

PR-161 ─► PR-162 ─► PR-164
   └────► PR-163 ─► PR-165 ─► PR-341/342

PR-181/182（可并行）
```
