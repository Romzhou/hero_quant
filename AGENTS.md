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
- `_FakeLLM`（`server.py:1690-1707` 同步 `/v1/query`、`server.py:2088-2105` 流式 `/v1/query/stream`）与 checkpoint emulated 判定（`_is_emulated_saver(server.py:124)`；降级落点 `server.py:1825-1832`、`server.py:2010-2023`）必须在响应中置 `degraded:true + reason`，前端必须横幅提示。
- 行号随 `server.py` 改动漂移，引用前一律 `grep -n` 复核，禁止照抄本文或审计文档里的旧行号。
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
- 分段提交：每段完成就 `git commit`（勿 `push`，由总管统一处理）；多线并行时 `git add` 必须带显式路径，禁止 `git add -A` 卷入他人文件；C 线提交信息前缀 `C:`（例 `C: docs/issues P0-P3 line mapping`）。
## 10. 依赖版本区间（新增依赖/调区间必须照此写）

| 场景 | 下界 | 上界 | 例 |
|---|---|---|---|
| 通用 | **本机实测版本**（没实测过的版本不许写下界） | 实测 major + 1 | `foo>=1.4.2,<2` |
| 存储/格式类库（自带文件格式，或版本号非严格 semver） | 实测版本 | 收一级 minor（major.minor+1） | `duckdb>=2.0,<2.1` |
| ABI 绑定类库（wheel 与解释器/上游 ABI 绑定） | 实测版本 | 实测 major + 1 | `pyarrow>=26,<27` |
| 拿不准归类 | 实测版本 | 取最窄（通常是 minor+1） | — |

- 下界一律写**实测过的版本**：写「理论上能用」的旧版本等于给未验证的运行时开门。
- 改区间必须同步 `uv pip compile --generate-hashes` 重生成 `requirements-lock.txt`，并跑 `pip-audit`；
  lock 里的版本要与 pyproject 区间自洽（历史教训：rich/pandas 曾长期与 pyproject 边界矛盾，靠 pip 自动降级掩盖）。
- 一次 PR 只动与本次需求相关的那几条，提交信息写明理由与被验证版本。
## 11. 完成口径与接线规则（2026-11-02 起）

**完成四级**（只有 L2 及以上可计入「完成」）：

| 级别 | 定义 | 判定证据 |
|---|---|---|
| L1 | 代码就绪 | 模块 + 单测存在，但没有任何入口能到它 |
| L2 | 接线可达 | 从 API 路由 / 工具注册 / CLI / 中间件 / 启动装配 / CI 作业能触达 |
| L3 | 路径覆盖 | 有端到端或真栈集成用例覆盖主路径（真实 DB/浏览器/进程） |
| L4 | 生产验证 | 目标环境跑过并留证（部署记录、演练记录） |

**接线测试三层**（只做第一层挡不住同名陷阱）：

1. **非 None**：装配后对象存在；
2. **来源精确匹配**：`type(obj).__module__` 必须等于期望模块（例：`hero_quant.memory.store`，不是 `hero_quant.agent.memory.store`）；
3. **行为探针**：调一个只有正确实现才具备的方法并断言可观测副作用（例：持久化实现能写穿到磁盘/DB）。

**红线三条**：

1. 禁止两个模块定义同名类（同名会导致"看起来接上了"的静默错误）。
2. 注释若描述「由谁负责」的机制，必须有代码路径佐证；无佐证一律删除。
3. 不许为了让测试变绿而弱化断言（删断言、放宽条件、加 skip）。
