# 摸底报告 hero-quant

**日期：2026-10-19**

**静态分析声明：** 本报告为纯静态分析产物，未安装任何依赖、未启动任何服务、未连接外部行情/LLM/数据库，未执行业务代码。所有结论来自对仓库文件（含源码、配置、文档、CI）的只读阅读与交叉核对。

**方法：** 由四路并行侦察（Lane-A 项目画像 / Lane-B 体量 / Lane-C 地形 / Lane-D 问题+AGENTS）分别取数，再由本报告合成去重。行号引用以当前 `master` 快照为准；凡涉密钥/演示数据/降级路径均标注“依据文件+行号”。唯一允许的写操作为创建/覆盖 `docs/recon.md`。

---

## 1. 项目画像摘要

**一句话定位：** hero-quant = 单机可部署的自然语言投研 Agent——一句话做回测，出 tearsheet + 证据账本。

**架构形态：**
- 后端 `FastAPI` 单进程 + 静态托管 Vite 构建产物（同源 8899 对外）。
- 内核 `AgentLoop`（`src/hero_quant/agent/loop.py:102` `AgentLoop.run`）+ LangGraph 可选分支（`src/hero_quant/agent/graph/`）。
- 数据层 `tencent/yahoo` 双源 + 16 源注册表 + 全链路 `provenance`（`src/hero_quant/data/`、`src/hero_quant/tools/registry/`）。
- 回测 `PIT` + 自研 `quantlib`（`src/hero_quant/quantlib/`、`src/hero_quant/backtest/`、`src/hero_quant/tools/market_data/`、`src/hero_quant/tools/backtest/`）。
- 记忆 `SQLite-FTS` + 治理 `hash` 链（`src/hero_quant/memory/`、`src/hero_quant/governance/`）。
- 可观测 `structlog` / `prometheus` / `OTel`（`src/hero_quant/telemetry/`）。
- 计费 `billing`（`src/hero_quant/billing/`），检查点 `checkpoint/postgres`（`src/hero_quant/checkpoint/`、`src/hero_quant/postgres/`）。
- 前端 React19 七路由（`frontend/src/`，`App.tsx` 路由表）。

**技术栈（锁定口径）：**
- `Python >=3.11,<3.14`（Docker 基座 `python:3.11-slim`，见 `Dockerfile`），`uv` + `requirements-lock.txt --require-hashes` 哈希锁。
- 前端 `TS5.7 + React19 + react-router7 + zustand5 + echarts5 + Vite6 + Tailwind3.4 + vitest3`，见 `frontend/package.json`。
- Rust `pyo3 0.21` stub（`crates/`，单 stub，无实质逻辑）。
- 关键后端依赖：`fastapi 0.141.1 / uvicorn / pydantic2 / pandas / numpy / scipy / httpx / langchain / structlog / redis / slowapi / psycopg`；extras 分组 `ashare / us / crypto / vector / dev`，见 `pyproject.toml`。

**入口与启动：**
- 后端唯一入口 `src/hero_quant/api/server.py:322 app=FastAPI`（模块级 `app` 对象）。
- 标准启动 `uvicorn hero_quant.api.server:app --port 8899`（`Dockerfile:103 CMD`）。
- 前端入口 `frontend/src/main.tsx -> App.tsx`，开发 `npm run dev --port 5173`。
- 单机交付 `docker compose up --build` 后 8899 同源服务（含静态前端回退）。
- 已知文档错误：`README.md:66` 的 `python -m hero_quant.api.server` 是错的——该包无 `__main__`，直接运行必失败（依据：`src/hero_quant/api/server.py` 无 `if __name__` 分支 + `src/hero_quant/api/__main__.py` 缺失）。

**服务与 Env：**
- 本地编排：`postgres:16` + `redis:7 volatile-lru` + `temporal1.26` 占位 + `otel-collector`，见 `docker-compose.yml`、`deploy/`（仅 otel）、`deploy/*.yaml`。
- 外部行情：`qt.gtimg.cn / yahoo + tushare / akshare / yfinance / ccxt`；LLM：`openai / deepseek / anthropic`，见 `src/hero_quant/llm/factory.py`、`src/hero_quant/llm/client.py`、`src/hero_quant/data/`。
- 唯一 env 网关 `src/hero_quant/config/settings.py`，键范围 `settings.py:146-347`，核心键：`HERO_LLM_PROVIDER / HERO_LLM_MODEL / HERO_LLM_API_KEY`、`HERO_DATA_MODE / HERO_DATA_MARKET`、`HERO_CHECKPOINT_DSN（默认 postgresql://localhost:5432/hero_quant）`、`HERO_BILLING_DSN（默认 None，fail-closed）`、`HERO_REDIS_DSN`、`HERO_VECTOR_*`、`HERO_WALL_TIME_BUDGET`、`PII_ENCRYPTION_KEY`、`HERO_OTEL_MODE`、`FRONTEND_DIST`。详见 `.env.example` + `settings.py:146-347`。

---

## 2. 体量摘要

**代码规模（静态计数）：**
- Python 296 文件：`src/` 117 文件 33439 行 + `tests/` 176 文件 20368 行（`def test_` 1301 个）+ `scripts/` 3 文件。
- 前端 25 文件 3788 行（17 `tsx` + 8 `ts`），见 `frontend/src/`。
- Rust 1 stub（`crates/`）。
- e2e 4 spec（`e2e/` 或 `tests/e2e`，playwright）。
- 合计约 5.7 万行；测试:源码约 0.6:1。

**模块划分：**
- `src/hero_quant` 下 24 子包；核心 8：`agent / api / data / quantlib / sandbox / memory / telemetry / billing + governance`。
- 外围：`frontend / crates / migrations(3 个 DDL) / docs(32 文件) / deploy（仅 otel） / skills(5) / scripts`。
- 下一级关键目录：`api(server/ws/security/risk) / agent(loop/trace/context/grounding/graph) / tools(registry/market_data/backtest) / llm(factory/client) / checkpoint / postgres / memory/store / billing / data / infra(redis/session) / config/settings / mcp / telemetry / governance / sandbox / backtest`。

**测试与覆盖率：**
- 后端 `tests/`：`pytest + pytest-cov + fakeredis`，`pyproject.toml` 中 `testpaths=["tests"]`。
- 前端 `vitest` 8 文件；e2e `playwright` 4 spec，但 config 只注册 3 project（1 个 spec 未被调度，依据：`playwright.config.ts` 的 `projects` 段 vs `e2e/*.spec.ts` 数量）。
- 覆盖率口径打架：`pyproject.toml fail_under=50`、`test.yml --cov-fail-under=50`、旧 `ci.yml` 46、`README` 写 30 已过期（依据各文件原文，后续以 50 为准，需同步文档）。

**CI：**
- 主门 `.github/workflows/test.yml`（blocking，除 playwright 外）：`hash-lock -> pip install -e .[dev,ashare,us] -> ruff check src -> frontend 三连（install/build/test） -> pytest cov50 -> pip-audit -> gitleaks`；含 `postgres:16` 服务容器。
- 另有拆分版 `ci.yml`（旧值残留）。
- `ruff target-py311 line120 select E,F,W`，仅 `check src`（未覆盖 tests/frontend）。
- 分支 `master`；无分支保护文件 / `CODEOWNERS` / `pre-commit`（依据：`.github/` 目录清单 + 根 `CODEOWNERS` 缺失 + `.pre-commit-config.yaml` 缺失）。

---

## 3. 地形摘要

**根目录地形：**
- `src/hero_quant/`（业务主体）/ `migrations/`（DDL）/ `frontend/` / `crates/` / `deploy/`（otel + yaml）/ `data/`、`memory/` 占位 / `docker-compose.yml` / `Dockerfile` / `pyproject.toml`。
- `src` 下一级（按链路重要度）：`api(server/ws/security/risk) / agent(loop/trace/context/grounding/graph) / tools(registry/market_data/backtest) / llm(factory/client) / checkpoint / postgres / memory/store / billing / data / infra(redis/session) / config/settings / mcp / telemetry / governance / sandbox / backtest`。

**链A — 问答（核心链路）：**
- `POST /v1/query/ticket`（`server.py:1235` 签票）-> `GET /v1/query:998` / `stream:1296`（`event_generator:1338`）。
-  gating：`_check_rate_limit -> consume_ticket -> Settings()`。
- 推理装配：`LLMFactory.create + model_for_stage -> bind_tools(get_definitions)`（`src/hero_quant/llm/factory.py`、`src/hero_quant/tools/registry/`）。
- 上下文与治理：`TraceWriter + GroundingLedger + ContextManager + BudgetBreaker + _get_checkpoint_saver`。
- 执行：`AgentLoop.run`（`src/hero_quant/agent/loop.py:102`）。
- 落盘与广播：`trace.jsonl` + ledger 内存 + checkpoint + WS 广播（`src/hero_quant/agent/trace/`、`src/hero_quant/api/ws.py`）。
- 依据：`src/hero_quant/api/server.py:998,1235,1296,1338` + `src/hero_quant/agent/loop.py:102`。

**链B — 回测 / 轨迹：**
- `GET /v1/backtest/metrics.json` 等（`server.py:1789 / 1805 / 1822 / 1925`）-> `_get_backtest_bundle:1721`：L1 内存 -> L2 Redis -> 重算 -> `_static` 回退。
- `GET /v1/trace/events:1948` 读 `TRACE_PATH` 等按 `offset` 切片。
- 依据：`src/hero_quant/api/server.py:1721,1789,1805,1822,1925,1948`。

**存储（无 ORM，手写驱动）：**
- `psycopg_pool + redis.asyncio / fakeredis`（无 SQLAlchemy/Django ORM，依据：`grep ORM/sqlalchemy` 无命中 + `src/hero_quant/postgres/` 手写池）。
- checkpoint：PG 表 `checkpoints`（`migrations/001_checkpoint.sql:5`），DSN 默认 `localhost`；无 PG 走 `memory:///` + `_PG_GLOBAL_STORE` 内存回退。
- billing：`factors / purchases` + RLS（`migrations/002_billing_rls.sql`），DSN `fail-closed`（`HERO_BILLING_DSN` 默认 None 则拒绝启动计费路径）。
- memory：`MemoryStore(base_path) -> memory.db FTS5 + 文件 + hierarchy`，向量 `pgvector` 失败回退（`src/hero_quant/memory/store.py`）。
- Redis 键前缀：`hero:ticket / hero:ratelimit / hero:cache / hero:stream`（依据：`src/hero_quant/infra/redis.py` + `server.py` 键字面量）。

---

## 4. 问题与 AGENTS 摘要

### 4.1 致命（P0，不修不准上线/不准合入）

1. **明文真密钥 + env_file 自动加载风险** — 依据：`.env:3-4` 存明文真密钥（`HERO_LLM_API_KEY` / `HERO_API_KEY` 疑似真值），且 `compose env_file` / `Settings` 会默认加载。风险：密钥进镜像层/git 历史/日志。处置：立即轮换 + 改用占位符 + `.env` 永不入库（检查 `.gitignore` 是否覆盖）。
2. **README 错误启动示例** — 依据：`README.md:66` `python -m hero_quant.api.server`；反证 `src/hero_quant/api/` 无 `__main__.py` 且 `server.py` 无 `__main__` 守卫。处置：改为 `uvicorn hero_quant.api.server:app --port 8899`（与 `Dockerfile:103 CMD` 对齐）。
3. **前端零构建本地假活** — 依据：`frontend/dist/` 零文件 + `.gitignore` 忽略 `dist`，而 `server.py:2062-2174` 静态托管 + `_static` 回退在无构建时返回假页面/空 bundle，健康检查仍绿。处置：无 `dist` 时 `/` 明确 503 + 提示 `npm run build`，或 Docker 多阶段必构建。
4. **缺配直接 raise RuntimeError（可用性断点）** — 依据：`src/hero_quant/checkpoint/postgres.py:246-248` 缺配直接 `raise RuntimeError`，上层无降级开关说明。处置：与 billing 一样明确 `fail-closed/fail-open` 策略并在 `/ready` 如实暴露（参考 4.3-4）。

### 4.2 严重（P1，本迭代必修）

1. **_FakeLLM 合成演示数无感知** — 依据：`src/hero_quant/api/server.py:1091-1401` `_FakeLLM` 在无 LLM key 时合成看似真实的投研结论/数字，前端无 `demo/degraded` 标识。风险：用户把演示数当实盘依据。处置：响应头 + body 强制 `degraded:true + reason`，前端横幅。
2. **PG 不可达跳 DDL 走 emulated 假持久化** — 依据：`src/hero_quant/api/server.py:229-235` PG 不可达时跳过 DDL 走内存 `emulated`，但日志/探针仍报 ready。风险：重启丢单/丢检查点。处置：`emulated` 必须打标并在 `/ready` + `/v1/*` 响应中显式声明。
3. **宽 except 吞错（三处）** — 依据：`src/hero_quant/api/server.py:381`、`server.py:606`、`src/hero_quant/api/risk.py:164,236` 的裸 `except Exception` 直接降级/放行。处置：收窄异常类型 + `structlog` 结构化记录 + 指标计数。
4. **双写死 localhost 与 compose 矛盾 + 旧口令默认** — 依据：`src/hero_quant/config/settings.py:172` + `src/hero_quant/api/server.py:214` 双写死 `localhost`，与 `docker-compose.yml` 的 `postgres` 主机名矛盾，容器内必连失败；`README.md:142` 旧口令默认与 compose 不一致。处置：DSN 默认改走 env 模板（compose 内用 `postgres`，本地用 `localhost`），README 口令与 compose 对齐。

### 4.3 一般（P2，择期修）

1. **`server.py` 30+ 处 `except pass / debug` 静默** — 依据：`grep -n "except.*pass\|logger.debug" src/hero_quant/api/server.py` 30+ 命中。处置：按链A/链B分级提为 `warning/error` + 脱敏字段。
2. **checkpoint postgres silent handled 无结构化字段** — 依据：`src/hero_quant/checkpoint/postgres.py` 的 `except` 分支仅字符串 log，无 `exc_info/error.code/dsn_host` 字段。处置：统一 `structlog`  schema。
3. **阈值与“277 passed”口径打架** — 依据：`pyproject.toml fail_under=50` vs `README` 30 vs 历史 “277 passed” 截图。处置：统一 50，README 更新，CI 以 `test.yml` 为准，归档 `ci.yml` 旧值 46。
4. **`/ready:729-891` 每次建 ThreadPoolExecutor 双探易误报** — 依据：`src/hero_quant/api/server.py:729-891` 每次 readiness 新建 `ThreadPoolExecutor` 同步探 PG+Redis，在 `mem2g/cpu1.5` 小机上易超时误标 unready。处置：复用全局池 + 缓存探针结果 + 区分 `liveness/readiness`。

### 4.4 卫生（P3，顺手修）

- 无 `AGENTS.md / CLAUDE.md / CONTRIBUTING.md`（依据：根目录 `glob AGENTS* CLAUDE* CONTRIBUTING*` 无命中）。
- `ruff` 仅 `E,F,W` 无 `mypy`（依据：`pyproject.toml [tool.ruff]` + 无 `[tool.mypy]`）。
- `src/hero_quant/data/akshare_loader.py:108 TODO` + `wall_time` 裸 `pass` + `memory/` 20 处 `pass` 无单测锁定（依据：`grep -n "TODO\|pass" src/hero_quant/memory/ src/hero_quant/data/akshare_loader.py`）。
- `docs/plans/` 被 `.gitignore` 大面积忽略，计划文档不在库内（依据：`.gitignore` 中 `docs/plans` 条目）。

### 4.5 AGENTS 缺失现状

- 现状：仓库无 `AGENTS.md`（亦无 `CLAUDE.md` / `CONTRIBUTING.md`），新人/代理无只读边界、env 门禁、启动验证、fail-closed 约束的可执行契约。本报告第 6 节附可直接落地的起草全文（7-10 条）。

---

## 5. 可并行拆分建议

**目标：** 切 5 条互不重叠的线，可同日并行开工、互不阻塞合入。以下按“实际文件归属”微调过：为避免同文件冲突，`src/hero_quant/api/server.py` 统一归线 D，线 A 不碰该文件（与初版“ A 含 server 入口”相比的唯一偏离，理由：同文件多线并发必冲突）；`README.md` 统一归线 A，线 C 不碰 `README.md`（只碰 `docs/ + AGENTS.md`）。

| 线 | 方向 | 能碰（白名单） | 不能碰（黑名单） | 验收 |
|---|---|---|---|---|
| A 修启动与配置 | settings 网关 + 容器启动 + README 启动章节 | `src/hero_quant/config/settings.py`、`Dockerfile`、`docker-compose.yml`、`.env.example`、`README.md`（仅启动/env 章节） | `src/hero_quant/api/server.py`、`src/hero_quant/checkpoint/`、`src/hero_quant/telemetry/`、`tests/`、`frontend/`、`e2e/`、`migrations/`、`.github/`、`docs/` | `uvicorn hero_quant.api.server:app --port 8899` 本地+容器双验证；compose 内 DSN 用 `postgres` 可连；README 无 `python -m` |
| B 补测试与CI口径 | 测试补齐 + 覆盖率/CI 单一口径 | `tests/`、` .github/workflows/test.yml`、` .github/workflows/ci.yml`（归档/对齐）、`pyproject.toml`（仅 `[tool.pytest] / [tool.coverage]` 段） | `src/`（业务源码一行不碰）、`docs/`、`README.md`、`frontend/`、`e2e/`、`migrations/`、`Dockerfile`、`docker-compose.yml` | `pytest --cov-fail-under=50` 绿；`ruff check src` 绿；README/ci.yml 旧 30/46 已清除；e2e 缺失 project 已补或注明 skip 原因 |
| C 写文档与AGENTS | 摸底沉淀 + 新人契约 | `docs/`（含本 `recon.md` 后续增补）、根 `AGENTS.md`（新建）、`CONTRIBUTING.md`（可选新建） | `src/`、`tests/`、`frontend/`、`e2e/`、`migrations/`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml`、`.github/`、`README.md`（交由线 A，避免同文件冲突） | `AGENTS.md` 7-10 条可执行；`docs/` 与第 4 节问题行号一一对应；不改任何代码行为 |
| D 重构可观测与异常处理 | 日志脱敏 + 异常收窄 + fail-closed 显式化 | `src/hero_quant/api/server.py`（唯一可改 server.py 的线）、`src/hero_quant/api/risk.py`、`src/hero_quant/checkpoint/`、`src/hero_quant/postgres/`、`src/hero_quant/telemetry/`、`src/hero_quant/governance/`、`src/hero_quant/infra/` | `src/hero_quant/config/settings.py`、`frontend/`、`e2e/`、`migrations/`、`tests/`、`docs/`、`README.md`、`.github/`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml` | `_FakeLLM/emulated` 必打 `degraded`；宽 except 已收窄；`/ready` 复用池+缓存；`structlog` 脱敏单测 |
| E 前端构建与E2E | 前端构建闭环 + E2E 配齐 | `frontend/`（含 `package.json/vite.config`）、`e2e/`（或 `tests/e2e/`）、`playwright.config.ts` | `src/`、`migrations/`、`docs/`、`README.md`、`AGENTS.md`、`.github/`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml`、`src/hero_quant/config/` | 无 `dist` 时后端明确 503（仅消费线 D 暴露的语义，不改 server.py）；`npm run build + preview` 绿；4 spec 全被调度 |

**重叠自检（文件级）：**
- `server.py`：仅 D 可改，A/B/C/E 禁碰。
- `settings.py / Dockerfile / compose / .env.example`：仅 A 可改，D 禁碰。
- `tests/ + .github/ + pyproject coverage 段`：仅 B 可改，D 禁碰 tests。
- `docs/ + AGENTS.md`：仅 C 可改，A/B/D/E 禁碰 docs。
- `frontend/ + e2e/ + playwright.config`：仅 E 可改，B/D 禁碰前端。
- `migrations/`：五线均禁碰（冻结，待后续专线）。
- `README.md`：仅 A 可改，C 禁碰（避免 A/C 同文件冲突）。

---

## 6. AGENTS.md 检查

**检查结论：无该文件。** 根 `glob AGENTS.md / AGENTS*` 无命中；亦无 `CLAUDE.md / CONTRIBUTING.md` 替代。以下为可直接写入根 `AGENTS.md` 的起草全文（共 9 条，含只读边界 / env gate / 启动验证 / 架构 / fail-closed / 风格 / PR）：

```markdown
# AGENTS.md — hero-quant 代理执行契约

> 适用对象：所有读写本仓库的人类与 AI 代理。违反任一条，review 直接打回。

## 1. 只读边界（默认只读，写前申报）
- 默认只做静态分析与最小复现；任何写操作前必须声明“要碰哪些文件、为什么非碰不可”。
- 禁止整仓格式化、批量重命名、跨线顺手修。一次 PR 只做一件事。
- `migrations/` 为冻结区：无专线指令不得新增/修改 DDL。

## 2. Env Gate（唯一入口 + 绝不碰真密钥）
- 唯一 env 网关是 `src/hero_quant/config/settings.py:146-347`；新增 env 必须同步 `.env.example` + `settings.py` + `README` env 表，三处缺一不可。
- 严禁读取/回显/提交 `.env` 真值；`.env:3-4` 类真密钥只轮换不传播。`HERO_BILLING_DSN` 为空即 fail-closed，不得静默旁路。
- `localhost` 与 `postgres` 主机名不得硬编码混用：本地默认 `localhost`，compose 内必须 `postgres`，经 `settings.py` 统一切换。

## 3. 启动验证（先跑起来，再改代码）
- 后端唯一启动：`uvicorn hero_quant.api.server:app --port 8899`（`src/hero_quant/api/server.py:322`）。`python -m hero_quant.api.server` 已废弃，禁止写入文档。
- 前端：`frontend/` 下 `npm run dev --port 5173`；交付以 `npm run build` 产物为准，无 `dist` 时后端必须显式 503，不得假活。
- 单机验收：`docker compose up --build` 后 8899 同源可访问 + `/ready` 绿。

## 4. Run-Verify（改完必须跑的门）
- 后端：`ruff check src` + `pytest --cov-fail-under=50`（`pyproject.toml` 为准，README 旧 30 作废）。
- 前端：`npm run build` + `npm run test`（vitest）；e2e 以 `playwright.config.ts` 注册 project 为准，4 spec 必须全调度或显式 skip。
- 安装：`uv` + `requirements-lock.txt --require-hashes`；`pip install -e .[dev,ashare,us]` 仅 CI 用，本地不得放宽哈希。

## 5. 架构（链路归属，不跨层直调）
- 链A问答：`server.py:998,1235,1296,1338 -> LLMFactory -> AgentLoop.run(loop.py:102) -> trace.jsonl + WS`；链B回测：`server.py:1721,1789,1805,1822,1925,1948` L1->L2->重算->_static。
- 数据访问走 `tools/registry + data/`，无 ORM（手写 `psycopg_pool + redis.asyncio`）；记忆走 `MemoryStore -> memory.db FTS5`，向量失败必须回退并打标。
- 新增工具/数据源必须注册表登记 + provenance 全链路透传，不得私开直调。

## 6. Fail-Closed（降级必须显式，不许假成功）
- `_FakeLLM(server.py:1091-1401)` 与 `emulated(server.py:229-235)` 必须在响应中置 `degraded:true + reason`，前端必须横幅提示。
- `checkpoint/postgres.py:246-248` 缺配 `raise` 与 `billing RLS` 拒绝必须透到 `/ready`，禁止 `except pass` 吞错；宽异常必须收窄 + `structlog` + 指标计数。

## 7. 风格（日志脱敏 + 行号依据）
- 日志统一 `structlog`，含 `trace_id / ticket_id / dsn_host(脱敏) / error.code`；PII/API key 永不进日志。
- 异常处理禁止裸 `except Exception: pass`；`server.py` 存量 30+ 处按链A/链B分级清理。
- 提问/报告必须带 `文件路径+行号+依据`，无依据的结论视为无效。

## 8. 测试与覆盖率口径
- `tests/` 为 pytest 唯一目录（`testpaths=["tests"]`），`fakeredis` 隔离外部；`fail_under=50` 为硬门，旧 `ci.yml:46` 与 README 旧值以 `test.yml + pyproject` 为准。
- `memory/` 的 20 处 `pass` 与 `akshare_loader.py:108 TODO` 需单测锁定一处、清理一处，不得新增裸 `pass`。

## 9. PR（文档同步 + 并行不撞车）
- PR 必须列出“能碰/不能碰”文件清单，遵守第 5 节五线划分：`server.py` 仅 D 线、`settings.py` 仅 A 线、`tests/.github` 仅 B 线、`docs/AGENTS` 仅 C 线、`frontend/e2e` 仅 E 线、`migrations/` 全线冻结。
- 改行为必改文档：`README` 启动/env、`docs/` 行号、`.env.example` 三处联动；`docs/plans` 被 ignore 的需显式说明去向。
```

---

第二棒待指令
