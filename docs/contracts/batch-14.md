# 批次 14 契约（2026-11-01）

> 基线：master `b37265c`（内含 `22b78b0`）。本文件是 W1~W7 的**唯一接口来源**；
> 与任务书冲突时以本文件为准，本文件没写的接口不许自创，先停下来报告。
> 主控独占：`pyproject.toml` / `requirements-lock.txt` / `db/migrations/` /
> `src/hero_quant/db/runner.py` 的 EXPECTED_TABLES / `api/server.py` 路由注册 / `config/settings.py` / 文档。
> 子 agent 一律不碰上述文件。

## 1. 全局约定

1. **降级必须显式**：任何拿不到真实数据的分支返回 `degraded: true` + 稳定 `reason`（蛇形小写），
   数值字段置 `null`。禁止 0 / 0.0 / epsilon / 默认价冒充真实值。
2. **不写死数字**：代码与测试里不许出现「看起来像真数据」的常量；确需固定值必须标注来源。
3. **测试落点**：`tests/test_pr<编号>_*.py`；需要真 PG 的用例在报告里单列，由主控登记到
   `scripts/run_pg_tests.py` 的 `_PG_TESTS`。
4. **日志**：structlog 或 stdlib logging，字段只带 `error_code` / 逻辑名 / host 等脱敏值，
   永不带密钥、口令、完整 DSN、prompt 原文。
5. **异常**：禁止裸 `except Exception: pass`；宽异常必须收窄 + 打日志。
6. **跑测**：只跑自己的子集 `pytest -q <你的 tests 路径>`（并发跑全量会抢本机 dev Redis 限流桶）。
7. **磁盘**：根分区约 1.5G 可用。不装包、不 `docker build`、不在仓库里生成大文件；
   PIT 样本预算 100M，超出即停并报告实测值。

## 2. 已落地 schema（主控已完成，子 agent 只读）

迁移：`db/migrations/0005_runs_queue.sql`、`db/migrations/0006_audit_chain.sql`；
执行器 `python -m hero_quant.db upgrade`（`src/hero_quant/db/runner.py`，带 advisory lock 与 drift 检测）。
新增表已登记进 `EXPECTED_TABLES`。

### 2.1 runs（0002 既有 + 0005 增列）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | text PK | 应用生成的 run_id |
| `tenant_id` | text NOT NULL | RLS 判据 |
| `session_id` | text NULL | **0005 起可空**：长任务不必挂会话 |
| `status` | text | queued / running / succeeded / failed / cancelled / cancelling / dead |
| `kind` | text NOT NULL DEFAULT 'agent' | agent / backtest / research |
| `attempt` | int NOT NULL DEFAULT 0 | **fencing token**，每次领取 +1 |
| `max_tries` | int NOT NULL DEFAULT 3 | 超限置 dead |
| `lease_until` / `heartbeat_at` | timestamptz | 租约与心跳 |
| `payload` | jsonb NOT NULL DEFAULT '{}' | 作业参数 |
| `result` | jsonb | 成功结果 |
| `error` | text | 最近一次失败原因 |
| `reason` / `cost` / `started_at` / `ended_at` / `created_at` | 0002 既有 | 不变 |

**状态机**：queued → running → succeeded（成功终态，对外事件名 `run.done`）；
cancelling → cancelled；失败重试回 queued，`attempt >= max_tries` → dead（max_tries=3 即最多领取 3 次）。
> 成功态沿用 0002 的 `succeeded`（链A `session/store.py` 已在写），**不新增 `done` 字面量**，
> 避免两套终态；对外 SSE 事件仍叫 `run.done`。

索引：`idx_runs_claim (created_at) WHERE status='queued'`、`idx_runs_lease (lease_until) WHERE status='running'`。

### 2.2 run_steps（幂等断点）

`PRIMARY KEY (run_id, step_name)`、`run_id REFERENCES runs(id) ON DELETE CASCADE`、
`status IN ('succeeded','failed')`、`attempt`、`output jsonb`、`error`、`created_at`。
写入一律 `INSERT ... ON CONFLICT (run_id, step_name) DO NOTHING`；恢复时先查已完成 step 跳过。

### 2.3 audit_log 增列 + audit_chain_head（0006）

- `audit_log.prev_hash` / `entry_hash`：INSERT 前算好随行写入；**迁移前历史行为 NULL**，
  明确表示「链外历史」，校验从首个非 NULL 行开始，禁止回填伪造。
- `audit_chain_head(tenant_id PK, head_id, head_hash, entries, updated_at)`：每租户链头，
  追加时 `SELECT ... FOR UPDATE` 串行化。

### 2.4 RLS 与 worker（决策：方案 A + SET LOCAL）

`runs` / `run_steps` / `audit_chain_head` 全部 `ENABLE + FORCE ROW LEVEL SECURITY`。
worker 跨租户领取靠 0005 的独立 permissive policy（与 tenancy policy 是 OR 关系），但**必须**：

1. `SET LOCAL app.queue_worker='on'` 只在**领取事务内**设置；**禁止 `SET`（会话级）**——
   连接回池后 GUC 仍在，下一个复用该连接的请求会获得跨租户可见性。
2. 领取成功后同一事务内立刻 `SET LOCAL app.tenant_id=<该行 tenant_id>`，执行期与写结果
   回归普通租户 RLS，不再带 worker GUC。`current_tenant()` 已由 0007 支持 `app.tenant_id` 别名。
3. API 侧连接若读到 `current_setting('app.queue_worker', true)` 非空 → **直接抛异常**
   （`runs/guards.py::assert_no_queue_worker`，异常 `QueueWorkerOnApiConnection(RuntimeError)`），
   不许静默继续。
4. 部署前置（写进 `docs/SECURITY-SECRETS.md` 与 `docs/production-readiness`，本批不建角色）：
   生产建独立角色 `hero_queue_worker`，把 0005 的 permissive policy 收窄为
   `TO hero_queue_worker`；**不要**给该角色 BYPASSRLS。


## 3. W1 · PR-122-a PIT（Parquet + DuckDB）

新模块 `src/hero_quant/data/pit_store.py`。**必须复用** `data/snapshot.py` 的
`normalize_bars` / `_enforce_quality`（若为私有则改用公开等价物并在报告说明）/
`snapshot_digest` 与既有异常类 `SnapshotNotFound` / `SnapshotValidationError` / `SnapshotQualityError`，
不许另起 ID 规则。

```python
class PitParquetStore:
    def __init__(self, root: str | pathlib.Path) -> None: ...
    def write_daily(self, tenant_id: str, vendor: str, adjust: str,
                    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], *,
                    fetched_at: datetime | None = None,
                    knowable_at: date | None = None) -> dict: ...
        # 清单：snapshot_id/sha256/row_count/storage_uri/rows/bytes/partitions/created
    def get_snapshot(self, tenant_id: str, snapshot_id: str) -> dict: ...
    def as_of(self, tenant_id: str, snapshot_id: str, symbol: str,
              as_of: date | str, *, start: date | str | None = None,
              end: date | str | None = None, limit: int = 5000) -> list[dict]: ...
    def list_snapshots(self, tenant_id: str, *, vendor: str | None = None,
                       symbol: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]: ...
    def dataset_stats(self, tenant_id: str | None = None) -> dict: ...
```

- 分区：`<root>/tenant=<t>/symbol=<s>/trade_date=<YYYY-MM-DD>/part-000.parquet`；
  清单：`<root>/_index/<snapshot_id>.json`（原子写：临时文件 + `os.replace`）。
- `as_of` 过滤 `trade_date <= as_of AND knowable_at <= as_of`，与 PG 版 `SnapshotStore.as_of` **同口径**；
  返回行形状同为 `symbol, trade_date, knowable_at, open, high, low, close, volume`。
- DuckDB 只做 `read_parquet` 内存查询，不落持久 `.duckdb` 文件（避免锁与体积）。
- 数据源走现有 `tools/registry` 或 `data/sources.py`；取不到就抛/记 degraded，不造假。
- **必须**有「未来函数」反证用例：构造一条 `knowable_at > as_of` 的修订行，断言不出现在结果里。

**根目录（决策）**：默认取 `hero_quant.config.settings.Settings().pit_root`，即 env `HERO_PIT_ROOT`，
默认 `/root/hero-data/pit`（**仓库外**）。
- `PitParquetStore(root=None)` 缺省用 `Settings().pit_root`；测试一律传 `tmp_path`，不读 env、不碰真实目录。
- 根目录**必须预先存在且可写**：否则构造时立刻抛 `PitStoreUnavailable(reason="pit_root_missing")` /
  `"pit_root_not_writable"`，**禁止 `mkdir` 兜底**（静默建目录会让「配错路径」看起来像成功）。
- 根目录下的子目录（`tenant=.../symbol=.../trade_date=...`）才由写入方创建。
- `HERO_PIT_ROOT` 的绝对路径校验（拒绝 `~`）在 settings 层完成；W1 不重复实现，直接用 `pit_root`。

## 4. W2 · PR-241 审计不可篡改

`src/hero_quant/audit/chain.py`：

```python
GENESIS_HASH: str  # "0" * 64
def entry_hash(prev_hash: str, row: Mapping[str, Any]) -> str
class AuditChain:
    def __init__(self, store: AuditStore) -> None: ...
    def append(self, tenant_id: str, action: str, **fields: Any) -> dict: ...   # 字段集同 AuditStore.append
    def head(self, tenant_id: str) -> dict: ...    # {head_id, head_hash, entries, updated_at}
    def verify(self, tenant_id: str, *, limit: int | None = None) -> dict: ...
        # {ok, entries, head_hash, broken_at, reason, degraded, reason_code}
```

- `entry_hash` 规范化顺序固定：`ts|tenant_id|action|resource|run_id|model|prompt_hash|result_hash|ip|detail(canonical json)`，
  不含自增 `id`（INSERT 前未知）；docstring 写清，单测锁死（同一行两次调用同值）。
- 追加：事务内 `SELECT ... FOR UPDATE` 锁 `audit_chain_head` → 算 hash → INSERT `audit_log` → UPSERT 链头。
- `verify`：按 `id` 升序重算，首个断点返回 `broken_at` + `reason`（`hash_mismatch` / `prev_mismatch`），
  **不许**抛异常代替返回值。
- **返回值必须含**：`chain_start_entry_id`（首个 `entry_hash` 非 NULL 的 `id`，无则 `None`）、
  `chain_start_at`（该行 `ts`，无则 `None`）、`uncovered_count`（`entry_hash IS NULL` 的历史行数）。
  语义：历史行不回填、不伪造哈希，链从 `chain_start_entry_id` 起算。
- 端点 `GET /v1/audit/verify`（加在既有 `audit/api.py` 的 router 上，主控无需再注册，
  `server.py:1073` 已 include），需 admin 权限（复用 `require_audit_admin`）；返回上面四个字段 +
  `ok/entries/head_hash/reason`。
- **措辞硬约束**：文档与前端**禁止**写「审计已启用」这类全量断言；必须写成
  「自 `<chain_start_at>` 起算，此前 `<uncovered_count>` 条未覆盖」。前端由 W7 落实。
- 保留策略：`src/hero_quant/audit/retention.py::retention_plan(tenant_id, *, older_than_days=365, now=None) -> dict`
  **只读**（返回可归档范围与条数），不删除（`audit_log` 有 append-only 触发器）；文档写明归档需
  显式 DISABLE TRIGGER 的人工步骤。
- 生产者 `src/hero_quant/audit/producers.py`（**只由主控接线**）：
  `record_tool_call_audit(...)` / `record_backtest_audit(...)` / `record_session_audit(...)`；
  action 常量 `tool.call` / `backtest.run` / `session.run`；失败返回 `None` 并打 warning，绝不抛。

## 5. W3 · PR-201~203 PG 队列

`src/hero_quant/runs/{__init__,store,steps,worker,api}.py`。

```python
class RunQueue:
    @classmethod
    def from_dsn(cls, dsn: str | None = None) -> "RunQueue": ...
    def enqueue(self, tenant_id: str, kind: str, payload: Mapping[str, Any], *,
                session_id: str | None = None, max_tries: int = 3,
                run_id: str | None = None) -> dict: ...
    def claim(self, *, lease_seconds: int = 300) -> dict | None: ...
    def heartbeat(self, run_id: str, attempt: int, *, lease_seconds: int = 300) -> bool: ...
    def finish(self, run_id: str, attempt: int, *, result: Any = None) -> bool: ...   # fencing
    def fail(self, run_id: str, attempt: int, *, error: str) -> dict: ...             # requeued / dead
    def request_cancel(self, tenant_id: str, run_id: str) -> dict: ...
    def reap_expired(self) -> dict: ...        # {requeued: int, dead: int}
    def get(self, tenant_id: str, run_id: str) -> dict: ...
    def list(self, tenant_id: str, *, status: str | None = None, limit: int = 50) -> list[dict]: ...

class StepLedger:
    def done_steps(self, run_id: str) -> set[str]: ...
    def record(self, run_id: str, tenant_id: str, step_name: str, *,
               status: str = "succeeded", output: Any = None,
               attempt: int = 0, error: str | None = None) -> bool: ...

class RunWorker:
    def __init__(self, queue: RunQueue, handler: Callable[[dict, Any], Any], *,
                 lease_seconds: int = 300, heartbeat_interval: float = 30.0,
                 poll_interval: float = 1.0) -> None: ...
    def run_once(self) -> bool: ...
    def run_forever(self) -> None: ...
    def request_stop(self) -> None: ...

router: APIRouter   # 主控注册，前缀 /v1/runs 与 /runs
```

- `claim` 必须一条语句：`WITH picked AS (SELECT id FROM runs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE runs SET status='running', attempt=attempt+1, lease_until=now()+make_interval(secs=>%s), heartbeat_at=now(), started_at=COALESCE(started_at, now()) FROM picked WHERE runs.id=picked.id RETURNING ...`
- `finish` / `fail` 必须带 `AND attempt = %s`；影响 0 行 → 丢弃写、打 `runs.write_fenced` 日志、返回 False。
- 心跳任务每 `heartbeat_interval` 续租；续租失败（被 fencing）立即停止该任务。
- 看门狗 `reap_expired()`：`status='running' AND lease_until < now()` → `attempt >= max_tries` 置 `dead`，否则回 `queued`。
- **RLS/GUC（硬性，见 `2.4）**：领取事务内 `SET LOCAL app.queue_worker='on'` → claim → 同一事务内
  `SET LOCAL app.tenant_id=<tenant_id>`；此后执行期/写结果的每个事务只靠租户 RLS。
  **禁止 `SET`（会话级）**；worker 用完的连接回池前不得残留 GUC。
  `runs/guards.py::assert_no_queue_worker(conn)` 在 API 侧每次取连接后调用，检测到
  `current_setting('app.queue_worker', true)` 非空即抛 `QueueWorkerOnApiConnection`。
- 取消：`request_cancel` 把 queued 直接置 `cancelled`、running 置 `cancelling`；worker 只在
  **step 边界**检查并转 `cancelled`。**验收口径：取消在 step 边界 5s 内生效**，不承诺打断进行中的单次 LLM 调用。
- handler 契约：`handler(run, ctx)`；`ctx.is_cancelled() -> bool`、`with ctx.step("name") as step:`
  （进入时查 `run_steps`，已完成则跳过并 `step.skipped=True`）。
- API：`POST /v1/runs` / `GET /v1/runs` / `GET /v1/runs/{id}` / `POST /v1/runs/{id}/cancel` /
  `GET /v1/runs/{id}/events`（SSE）。全部走既有 auth 依赖，tenant 从令牌取。
- SSE 事件枚举：`run.queued` / `run.running` / `run.progress`（`{step, done_steps, total_steps}`）/
  `run.done` / `run.failed` / `run.cancelled`；另每 15s 一条 `: keep-alive` 注释行。
  事件来源 = 轮询 `runs` + `run_steps`（不新增事件表）。
- **三条硬门测试**：subprocess + SIGKILL 真杀进程后续跑；3 worker 并发抢 10 任务（每个恰好执行一次）；
  僵尸 worker 写入被 fencing 拒绝（影响 0 行）。
- **RLS 三条附加测试**：① 同一连接在**事务外**读 `current_setting('app.queue_worker', true)` 必须为空
  （证明用的是 SET LOCAL 而非 SET）；② 领取事务内带 GUC 时跨租户可见、事务提交后不可见；
  ③ API 路径拿到带 `app.queue_worker='on'` 的连接时 `assert_no_queue_worker` 抛
  `QueueWorkerOnApiConnection`（不许静默）。

## 6. W4 · PR-112 定价层

`src/hero_quant/pricing/{base,catalog_provider,backfill}.py`：

```python
@dataclass(frozen=True)
class PriceCard:
    model_id: str; input_usd_per_mtok: float; output_usd_per_mtok: float
    effective_from: date; source: str; currency: str = "USD"

class PricingProvider(ABC):
    backend: str
    def price(self, model_id: str, *, at: date | None = None) -> PriceCard | None: ...
    def list_cards(self, model_id: str | None = None) -> list[PriceCard]: ...

class NullPricingProvider(PricingProvider):    # backend="null"；price() 恒 None
class CatalogPricingProvider(PricingProvider): # backend="catalog"；基于 llm/catalog.MODEL_CATALOG
def get_pricing_provider(backend: str | None = None) -> PricingProvider
def backfill_pricing(*, since: date | None = None, dry_run: bool = True) -> dict
    # 桩：{"updated": 0, "skipped": 0, "reason": "pricing_source_unavailable"}
```

- `llm/cost.py::LLMUsage` 增：`usage_source ∈ {"provider","estimated","absent"}`、
  `estimated_method`、`input_chars/output_chars/calls/duration_s`（免费源原始指标）；
  `cost` 改为 `{"value": float|None, "status": "priced"|"unpriced"}`，`to_dict()` 同步。
  **保留**现有 `cost_usd` 属性（value 别名）与 `estimated` 字段以兼容既有调用方，二者必须一致。
- 拿不到价记 `unpriced`，**禁止** 0 元；拿不到 usage 不许伪造 estimated token，改记字符数。
- `ModelInfo` 增 `effective_from`（时点语义），旧条目给合理默认并在报告列明。
- 回归门改按「次数/耗时」：只改 `scripts/eval_*.py` 与对应测试，**不动** `docs/production-readiness`。
- 前端「未定价」展示由 W7 消费 `cost.status`；W4 只保证 API 字段与测试。

## 7. W5 · PR-234 回测现实性

`src/hero_quant/backtest/realism.py`：

```python
@dataclass(frozen=True)
class TradingCosts:      # bps = 万分之一
    commission_bps: float = 3.0; slippage_bps: float = 2.0; stamp_duty_bps: float = 5.0
@dataclass(frozen=True)
class TradingConstraints:
    price_limit_pct: float | None = None     # None = 不限制（默认保持既有行为）
def apply_slippage(price: float, side: str, *, bps: float) -> float
def apply_price_limit(prev_close: float, target: float, *, limit_pct: float | None) -> float
def is_halted(bar: Mapping[str, Any]) -> bool          # volume == 0 视为停牌
def delisted_after(bars: Sequence[Mapping[str, Any]]) -> date | None
def buy_and_hold(prices: pd.Series) -> dict            # {total_return, max_drawdown, series}
def realism_report(*, fills, bars, costs: TradingCosts, constraints: TradingConstraints) -> dict
```

- **默认行为不变**：`BacktestEngine.run(costs=0.0005)` 的既有语义与非 realism 路径完全不动；
  新能力只通过显式参数/新函数启用。
- 数据不足（无成交量、无前收盘）→ `degraded=true + reason`（`volume_missing` / `prev_close_missing`），
  不猜、不补零。基准缺行情时 `benchmark=null` + reason。

## 8. W6 · PR-151 收尾

- `src/hero_quant/secrets/redis.py::apply_redis_password(dsn: str | None) -> str | None`
  （与 `settings._apply_pg_password` 同规则：只给「有 userinfo、无口令」的 `redis://` / `rediss://` 注入，
  口令百分号编码，取不到口令原样返回）。**不改 settings.py**：主控在批次 4 接线。
- `src/hero_quant/secrets/audit_bridge.py::rotation_audit_hook(chain) -> Callable[[dict], None]`
  把轮换事件（`secrets.rotation.begin` / `secrets.rotation.complete`）写进审计链；只带
  name/version/fingerprint，**绝不带值**。接线同样由主控做。
- 测试：vault/kms 不注入、口令特殊字符编码、日志无真值、hook 失败不抛。

## 9. W7 · PR-215 管理后台

- 后端 `src/hero_quant/admin/api.py`（`APIRouter`，前缀 `/v1/admin` 与 `/admin`）：
  `GET /v1/admin/overview` / `GET /v1/admin/tenants` / `GET /v1/admin/runs`（只读看板，支持 status/limit）/
  `GET /v1/admin/quotas` / `GET /v1/admin/audit/summary` / `GET /v1/admin/toggles` + `POST /v1/admin/toggles`。
  全部要求 admin 角色（复用既有 auth 依赖，参考 `audit/api.py::require_audit_admin`）。
- 前端 `frontend/src/pages/admin/Admin.tsx`（+ 子组件），路由 `/admin` 写进 `frontend/src/App.tsx`；
  批次 3 只有 W7 一个前端写者，允许改 `App.tsx` 的路由与导航。
- 无数据显示 `--` 与 reason；不许 `NaN%`、不许写死示例数字；成本展示读 `cost.status`（未定价 ≠ 0）。
- 审计区措辞**禁止**「审计已启用」，必须照 `GET /v1/audit/verify` 的 `chain_start_at` /
  `uncovered_count` 写「自 … 起算，此前 N 条未覆盖」。

## 10. 文件域（越界即停）

| Agent | 独占 |
|---|---|
| W1 | `src/hero_quant/data/pit_store.py`、`tests/test_pr122_pit_*.py` |
| W2 | `src/hero_quant/audit/**`、`tests/test_pr241_*.py` |
| W3 | `src/hero_quant/runs/**`、`tests/test_pr201_*.py` / `test_pr202_*.py` / `test_pr203_*.py` |
| W4 | `src/hero_quant/pricing/**`、`src/hero_quant/llm/cost.py`、`src/hero_quant/llm/catalog.py`、`scripts/eval_*.py`、`tests/test_pr112_*.py` |
| W5 | `src/hero_quant/backtest/**`、`tests/test_pr234_*.py` |
| W6 | `src/hero_quant/secrets/**`、`tests/test_pr151_redis*.py` |
| W7 | `src/hero_quant/admin/**`、`frontend/src/pages/admin/**`、`frontend/src/App.tsx`（仅路由/导航）、`frontend/src/__tests__/admin*.test.tsx` |

## 11. 主控在批次 4 统一做

1. `api/server.py` 注册 `runs.router`、`admin.router`（`/v1/...` 与无前缀双注册）。
2. `config/settings.py` 接 `apply_redis_password`；`secrets/rotation` 接 `rotation_audit_hook`。
3. `audit/producers.py` 接进工具级 / 回测级 / 会话级调用点。
4. `scripts/run_pg_tests.py` 的 `_PG_TESTS` 登记队列 PG 用例。
5. `progress-2026-10-29.md`、`backlog.md`、`README`、`docs/SECURITY-SECRETS.md`、openapi 快照。
