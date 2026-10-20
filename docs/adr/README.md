# ADR（架构决策记录）

> 依据：`docs/recon.md` §1/§3（无 ORM / fail-closed / 双源 / FTS+pgvector）、`docs/issues.md` P0-4/P1-2/P1-4。
> 状态说明：以下 4 条为对既有实现的**追认记录**（现状已如此），不是新决策；每条含背景/决策/后果。新增 ADR 请按 `ADR-NNN` 续号、附文件+行号依据。
> ADR-005 / ADR-006 为**新增的决策记录**（方向已定、本轮未实施，零代码改动）：ADR-005 记 billing tenant 归属（G-03 决策记录），ADR-006 记 per-user 身份与鉴权重构（G-02 残余面决策记录）。

---

## ADR-001：无 ORM，手写 `psycopg_pool + redis.asyncio` 驱动

- **状态**：已采纳（追认）。
- **背景**：后端是 FastAPI 单进程 + PG/Redis 双存储（checkpoint 表 `migrations/001_checkpoint.sql:5`、billing 表 `migrations/002_billing_rls.sql:10,20`、Redis 键 `hero:ticket/ratelimit/cache/stream` 见 `src/hero_quant/infra/redis.py:26-27`），但全仓 `grep ORM/sqlalchemy` 无命中（`docs/recon.md:93`）。checkpoint 需要线程/异步双池、租户键推导（`src/hero_quant/checkpoint/postgres.py:178-223` `_pg_store_key/_pg_lock_key`）、`MissingRunMapping` fail-closed（`:252`）等细粒度语义，ORM 抽象反而碍事。
- **决策**：不引入 SQLAlchemy/Django ORM；PG 走 `psycopg_pool` 手写池（`src/hero_quant/postgres/` + `checkpoint/postgres.py:564` `AsyncPostgresSaver`），Redis 走 `redis.asyncio`（`infr
a/redis.py:213,281` `get_redis_sync/get_redis`），SQL 手写、键前缀手写。
- **后果**（正面）：DSN/鉴权统一收口 `settings.py`（见 ADR-002）；`_is_real_pg_pool` 判据（`server.py:124` 注释“pool 缺失或非 real 即 degraded”）可精确区分 emulated；RLS 复合 FK（`002_billing_rls.sql:27-28`）手写可控。
- **后果**（代价/约束）：SQL 注入面与连接管理靠 code review 保障；`migrations/` 冻结（AGENTS §1），DDL 变更须专线；`fakeredis` 为测试唯一隔离手段（`testpaths=["tests"]`，AGENTS §8）。

## ADR-002：Billing `HERO_BILLING_DSN` 为空即 fail-closed

- **状态**：已采纳（追认）。
- **背景**：计费是强一致记账（`factors/purchases + RLS`，`migrations/002_billing_rls.sql:37-38` `ENABLE+FORCE ROW LEVEL SECURITY`），一旦在无持久化时伪装成功即丢单/资损。`src/hero_quant/config/settings.py:204` 网关注释“仅显式 `HERO_BILLING_DSN` 生效；默认 fail-closed 返 None”；`server.py:229-235` 曾有 PG 不可达跳 DDL 走内存 emulated 且探针仍 ready 的假持久化先例（P1-2）。
- **决策**：空 DSN → `billing_dsn=None`（`settings.py:204-214`）；`BillingService` 无真池时 `_pg_publish_sync/_pg_purchase_sync` 返 `False`、查询桩返 `None`（`service.py:300-312,569-574,1043-1094`），调用方抛 `RuntimeError`（`:444`），不写内存、不追加 ledger（`:385`）；`/ready` 以 `reason="billing DSN 缺配 fail-closed(memory)"` + `503 + degraded:true` 显式暴露（`server.py:953-974`）。
- **后果**（正面）：资损面归零——假成功不可能；`/ready` 可判读（见 `docs/runbook.md` §2）。
- **后果**（代价/约束）：本地无 PG 即无计费路径（须配 `HERO_BILLING_DSN` 或接受降级）；`settings.py:220-228` 回退链（`HERO_PG_DSN/HERO_CHECKPOINT_DSN` 共享 DB）会打 `warnings.warn + logger.warning`，混用部署必须显式知晓共享 DB 事实；与 memory 向量“静默回退”语义相反（见 ADR-004），新人须区分“钱”与“缓存”的不同降级等级。

## ADR-003：双源行情 `tencent` 主 + `yahoo` 备，provenance 全链路透传

- **状态**：已采纳（追认）。
- **背景**：外部行情 `qt.gtimg.cn / yahoo + tushare / akshare / yfinance / ccxt`（recon §1；`llm/factory.py`、`data/`），16 源注册表（`data/registry.py:1-5`）。tencent（手）与 yahoo（股）存在 100x 单位差（`registry.py:586`），tencent/akshare live 为硬编码 qfq 前复权（`registry.py:97,435-436`）。单源故障与口径混用都曾是实盘风险。
- **决策**：主备双源（`data/sources.py:7,9`），`TencentLoader.get_bars`（`loaders/tencent.py:97`）主、`YahooLoader.get_bars`（`loaders/yahoo.py:33`）备；所有返回经 `_resolve_provenance(:56)` + `_provenance_adjust(:432)` 打上 `Provenance{source, unit, adjust, factor_asof}`；手/股冲突直接 `CrossSourceError(:83)` 阻断（`:586` “不得换算放行”）；`bench` 合成路径强制 `non-PIT`（`backtest/bench.py:6,50,57`）。
- **后果**（正面）：故障切源不断链，且消费者永远知道数从哪来、什么单位、什么复权；`tools/registry.py:239` `get_definitions` 透传后 LLM 侧亦可见 provenance。
- **后果**（代价/约束）：新增源必须实现 `SourceTrait`（`data/trait.py:21,32,52`）+ `register(:138)` 登记 + provenance 映射（`_infer_loader_source:487-493` 类名映射须同步），不得私开直调（AGENTS §5）；`akshare_loader.py:108` 单位 TODO 在确认前不得私自换算。

## ADR-004：记忆 `SQLite-FTS5` 主索引 + `pgvector` 可选回退（与 billing 相反的降级等级）

- **状态**：已采纳（追认）。
- **背景**：记忆是“检索增强”而非“资损面”：文件已是真实来源（可审计），向量只是召回增强。`memory/store.py:1-4` docstring 定调“文件为真实来源，FTS5 为检索索引；向量本地优先、pgvector 失败静默回退”；pgvector 鉴权收口 Settings（`:61-88` 前缀白名单 + `_pgvector_dsn(:65)`）。
- **决策**：主索引 `SQLite FTS5`（BM25，trigram 优先 `:48`，建表 `:739-777`）；向量本地优先，`PgVectorSidecar`（`:123-130`）仅在 `is_pgvector_configured()(:79)` 且就绪时为一等公民（upsert/query 双写/双读 `:502-519`），任一失败 `logger.debug("silent handled: offline-safe...") + pass # intentional offline-safe`（13 处，见 `docs/arch/memory.md` §2），`vector_backend()` 自述 `pgvector/local`（`:643-646`）。
- **后果**（正面）：离线/无 PG 单机开箱即用；pgvector 故障不影响问答主链（与 ADR-002 的 billing fail-closed 形成“钱严、忆宽”的分级降级体系）。
- **后果**（代价/约束）：静默回退点的可观测性弱（`debug` 级别，P2-1 候选中）；`memory/` 20 处量级 `pass`（`lifecycle.py/hierarchy.py/store.py`，见 `docs/issues.md` P3-3）中属于本 ADR 的回退 `pass` 须保留语义并补单测，不得当卫生问题误删；`wall_time` 裸 `pass` 不属本 ADR，优先清理。

---

## ADR-005：billing tenant 归属（`purchases.tenant` 卖家列与账本归因）

- **状态**：已定方向，未实施（本轮仅文档）。本 ADR 是 `docs/security-audit.md:36` G-03 的**决策记录**；本轮不改 `src/`、不改 `migrations/`（冻结区，AGENTS §1）。
- 详见 [ADR-005.md](./ADR-005.md)：覆盖 billing 账本 / checkpoint 租户键推导 / `_Saver` 双写三条写入路径的 tenant 归属现状、G-03 触发路径与资损面、4+1 步可回滚迁移路径（其中存量回填一步需 DDL，须另立专线并由用户批准）、每步风险与回滚判据。

## ADR-006：per-user 身份与鉴权重构（从单共享密钥 + 匿名票到 per-user 凭证）

- **状态**：已定方向，未实施（本轮仅文档）。本 ADR 是 `docs/security-audit.md:25` G-02 残余面与 `docs/redteam-round2.md:61-70` M2-06 残余记账的**决策记录**；本轮不改 `src/`、不改 `tests/`、不新增 env。
- 详见 [ADR-006.md](./ADR-006.md)：覆盖 `HERO_API_KEY` 单密钥 → 匿名票 → 共享密钥互信的完整链路现状、per-user 目标模型（建议/待定）、5+1 步迁移路径与 `HERO_AUTH_MODE` 回退开关、对 G-02 / G-06 / G-08 / G-11 已落地语义的衔接（不推翻）、冒充面与审计缺口等风险。
