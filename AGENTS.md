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
