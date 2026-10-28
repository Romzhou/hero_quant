# 真英雄量化 · hero-quant

<p align="center">
  <b>自然语言 → 行情 → 回测 → 报告</b> · 单机可部署的极简投研 Agent
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/FastAPI-0.104+-009688" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react" alt="React 19" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT" />
  <img src="https://img.shields.io/badge/Tests-2767_passed-brightgreen" alt="tests" />
  <img src="https://img.shields.io/badge/Security-Headers_nosniff_frame--deny-blue" alt="Security Headers" />
  <img src="https://img.shields.io/badge/PII-Fernet_masked-informational" alt="PII" />
  <img src="https://img.shields.io/badge/Audit-tool_call_structured-success" alt="Audit" />
</p>

> 批判性借鉴 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 8 大工程模式（AgentLoop 状态机 / 上下文折叠 / Grounding 证据账本 / Tool 自动发现 / 行情 Fallback+Provenance / 回测引擎 / 治理 Hash 链 / 供应链契约），绿地重写核心业务，单进程 + Docker 单机可部署。

## 特性

- **投研闭环**：一句话完成 `600519.SH 近一月等权回测` → 自动拉行情 → PIT 校验 → 跑回测 → 出 `positions.csv/fills.csv/metrics.json/tearsheet.html` + 证据账本
- **数据保真**：`tencent`(A股 `board_lots`) / `yahoo`(美股 `shares`) 双源 + `16 源注册表`，`provenance{source,unit}` 全链路记录；跨源收盘价 1% 阈值校验已实现，但默认双源市场/单位互斥，**生产路径不触发**（数据层无熔断器，注册表是顺序回退、无打分）
- **回测正确性**：PIT `weights_on ≤ price_date` 正逻辑、非正价格/混币种拒绝、按换手计费、多标权重、月度热力 tearsheet
- **Agent 可靠性**：10 控制点状态机（`max_iterations/token_limit/TRUNCATED/user_stop/Retry•BudgetBreaker/Tool审计/Grounding/Context折叠/Trace 50k侧车`），`LangGraph StateGraph(plan→execute→verify)` 研究团队可选（`use_graph=True`）
- **工具**：`@tool(parameters/output/is_concurrency_safe)` 语义化合约，`get_market_data / run_backtest / technical_indicators / quantlib_call` 等 15 核心工具，读 True 写 False 并发审计
- **Quantlib**：`sma/ema/rsi(Wilder EWM)/bollinger/macd/max_drawdown` 纯 pandas，已对齐 8-11 RSI 修复
- **可观测**：`structlog JSON + X-Request-ID + /metrics(prometheus)`，`TraceWriter` `tmp→fsync→link` 硬链侧车 + `_safe_sidecar_path`
- **治理**：`governance/ledger.py` `seq/prev_hash/record_hash 0600+fsync` 可 `verify()`，`dedup` 幂等表（单机 dict，预留 PG）
- **前端**：React 19 + Vite + Zustand + Tailwind，9 条路由（`/`→`/dashboard` 重定向 + 8 条：`/dashboard` `/research` `/chat` `/backtest`(复用同一 Chat 组件) `/live` `/monitor` `/risk` `/settings`）；`Research` 读真实回测产物（`metrics.json`/`positions.csv`/`tearsheet.html`），`positions.csv` 无 `close` 列时显示诚实空态而非造曲线，请求失败回退带 `isMock` 标记；`Chat` SSE 流式 + tool 轨迹

## 架构

```
src/hero_quant/
  agent/      loop(状态机) / context(折叠) / grounding(证据) / trace(侧车) / graph(StateGraph) / policies(Retry/Budget)
  data/       registry(16源) + loaders/tencent(yahoo)
  backtest/   engine / metrics / validation(PIT)
  quantlib/   indicators(sma/ema/rsi/bollinger/macd)
  tools/      registry(@tool) + market_data/backtest/quantlib_tool(15工具)
  memory/     file + SQLite FTS5(trigram) + 30s 去重
  governance/ ledger(hash链) / dedup
  api/        FastAPI + SSE(/v1/query/stream) + StaticFiles(frontend/dist)
  checkpoint/ postgres(AsyncPostgresSaver memory/PG) + temporal(占位)
frontend/     Vite + React 19 + Zustand + ECharts
```

## 快速开始

```bash
# 1. 安装
pip install -e ".[dev,ashare,us]"
# ashare: tushare+akshare, us: yfinance, crypto: ccxt

# 2. 配置（可选）
cp .env.example .env  # 必须填 HERO_PG_PASSWORD，否则 DSN 渲染成空口令、/ready 如实 503（详见「Docker 单机部署」）
export HERO_LLM_PROVIDER=openai
export HERO_API_KEY=sk-...
export HERO_DATA_MODE=live   # synthetic | live  (live 才调 qt.gtimg.cn/yahoo)
export HERO_LLM_MODEL=gpt-4o-mini
export HERO_ENV=development  # 本地验收必须：无真 Redis 时限流器只在 development 允许 fakeredis 回退，否则 fail-closed 直接 429

# 3. 跑测试
pytest -q

# 4. 启动（唯一入口，旧式模块写法已废弃，见 Dockerfile CMD）
uvicorn hero_quant.api.server:app --host 0.0.0.0 --port 8899
open http://127.0.0.1:8899          # 前端：托管的是 frontend/dist 构建产物；陈旧需 cd frontend && npm run build。dist/index.html 缺失时返回 503 + X-Frontend（fail-closed，不假活）
open http://127.0.0.1:8899/docs     # Swagger
curl http://127.0.0.1:8899/live    # liveness
curl http://127.0.0.1:8899/metrics # prometheus
```

### 前端本地开发

```bash
cd frontend && npm ci && npm run dev -- --host --port 5173
# VITE_API_URL 默认同源，开发时代理到 http://127.0.0.1:8899
```

## Docker 单机部署

```bash
# 0. 必做：.env 从示例复制并填 PG 口令（不填 → DSN 渲染成空口令 → /ready 如实 503）
cp .env.example .env
# 编辑 .env：HERO_PG_USER=<llm-pg 容器内 $POSTGRES_USER，不是 postgres>
#            HERO_PG_PASSWORD=<真实口令，只写 .env，永不进仓库>

# 1. 首次部署 / llm-pg|llm-redis 容器被重建后：接入外部网络（幂等，可重复执行）
bash scripts/init/setup-shared-network.sh

# 2. 起服务
docker compose up --build
# http://127.0.0.1:8899  前后端同源（api 静态托管 frontend/dist）
# 数据落盘：named volumes hero-runs / hero-sessions / hero-home
# 硬化：cap_drop ALL + SETUID/SETGID(供沙箱) + no-new-privileges + read_only + tmpfs /tmp

# 仅构建镜像（单容器运行时需自行保证 PG/Redis 可达；建议直接用 compose）
docker build -t hero-quant:0.2.0 .
```

> **主机名口径（本轮最大的坑，两种运行方式不可混用）**
> - **宿主机直跑** `uvicorn hero_quant.api.server:app --port 8899`：PG/Redis 只能写 `100.114.54.26`。`127.0.0.1:5432/6379` **不通**——这两个实例只绑 tailscale IP。
> - **compose 容器内**：只能写 `llm-pg` / `llm-redis`。它们由别的 compose project 创建，需先跑 `scripts/init/setup-shared-network.sh` 建 `heroquant-shared` 外部网络并连入（带别名）；容器重建后网络成员关系丢失，需重跑。
> - `llm-pg` 的超级用户名**不是** `postgres`，取容器内 `$POSTGRES_USER`。
> - 外部 PG 首次使用需先建库 `heroquant` 并执行 `scripts/init/001_schema.sql`（`local-stack` profile 由 initdb 自动执行；复用 llm-pg 时手动执行一次）。
> - `--profile local-stack` 会另起 postgres/redis/temporal，此时必须在 `.env` 显式把主机名改成 `postgres`/`redis`——compose 的 `environment` 优先级高于 `env_file`，无法按 profile 条件覆盖。

### /ready 与容器健康

- `/ready` 聚合探测 PG（checkpoint + billing）与 Redis，全程 <5s 有界；**缺 checkpoint DSN 或库不可达即 503** + `degraded:true` + `reasons`（这是诚实降级，不是 bug）。
- 缺 `COHERE_API_KEY` 默认只 warning；只有 `HERO_READY_COHERE_FATAL=true` 才判致命。
- `Dockerfile:108-109` 的 `HEALTHCHECK` 已从 `/live` 改为 `/ready`——容器 `healthy` 现在代表**依赖可用**，不再只是进程活着。
- 前端唯一可信产物是 `frontend/dist`：dist 陈旧（缺新路由 chunk）必须 `npm run build`；**dist 目录或 `index.html` 缺失时返回 503**（`X-Frontend: missing|index-missing` + `Retry-After: 30`，`server.py:2851-2863`），不再用 200 占位页让编排层误判健康。

`Dockerfile` 3 阶段：`node:22-slim` 前端构建 → `python:3.11-slim` builder(`venv+hash-lock`) → `runtime`(无编译器，`vibe:vibe-sandbox 10001` 非 root)。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `HERO_LLM_PROVIDER` | `openai` | `openai` / `deepseek` / `anthropic` 等 |
| `HERO_LLM_MODEL` | `gpt-4o-mini` | 默认模型名 |
| `HERO_LLM_MODEL_QUICK` / `HERO_LLM_MODEL_DEEP` | 空（回退 `HERO_LLM_MODEL`） | 快/深两档独立模型槽位 |
| `HERO_API_KEY` | — | LLM 密钥；缺省走 `_FakeLLM` 合成路径，响应带 `degraded:true` |
| `COHERE_API_KEY` | — | 缺省只 warning，不影响 `/ready` |
| `HERO_READY_COHERE_FATAL` | `false` | `true` 才把缺 Cohere key 判为致命 degraded |
| `HERO_ENV` | 空 | 本地验收设 `development`，无真 Redis 时才允许 fakeredis 回退；否则限流 fail-closed 直接 429 |
| `HERO_DATA_MODE` | `live` | `live` 调真实行情；`synthetic` **可用**，但 `provenance.source=synthetic`、A股 `unit=board_lots`，跨源 1% 比对被显式跳过并 warning（非"离线可用"） |
| `HERO_DATA_MARKET` | `CN` | 默认市场 |
| `HERO_BENCHMARK_TICKER` | 空 | 显式基准；空则按 `benchmark_map` 后缀映射（`.NS`→`^NSEI` 等） |
| `HERO_PG_HOST` | `localhost` | 宿主机直跑填 `100.114.54.26`；compose 内由 compose 固定为 `llm-pg`（显式 `HERO_*_DSN` 优先） |
| `HERO_PG_USER` / `HERO_PG_PASSWORD` | 空 | 供 compose 的 DSN 变量插值；llm-pg 超级用户名**不是** `postgres`，取容器内 `$POSTGRES_USER`；口令只写 `.env` |
| `HERO_PG_DSN` | 空 | checkpoint DSN 的 legacy 别名（显式 `HERO_CHECKPOINT_DSN` 优先） |
| `HERO_CHECKPOINT_DSN` | `postgresql://localhost:5432/heroquant`（无口令） | 显式优先；compose 内渲染为 `postgresql://<user>:<password>@llm-pg:5432/heroquant`。**`.env` 不填 `HERO_PG_PASSWORD` 时口令段为空**，`/ready` 如实 503 |
| `HERO_CHECKPOINT_TTL_SECONDS` | `604800`（7 天） | checkpoint `expires_at` TTL |
| `HERO_BILLING_DSN` | 空（fail-closed） | 不配即计费不可用，`publish/purchase` 抛 `BillingUnavailable` |
| `HERO_BILLING_ALLOW_SHARED_DB` | `false` | billing 复用 checkpoint 库必须显式 opt-in，否则返回 None |
| `HERO_BILLING_ALLOW_MEMORY` | `false` | `true` 才允许无 PG 走进程内计费，且响应必须带 `degraded:true` |
| `HERO_REDIS_DSN` | 空 | 优先级最高；其次 `HERO_REDIS_HOST/PORT/PASSWORD/DB`（默认 `6379/0`），再次 `REDIS_URL` |
| `HERO_VECTOR_STORE` / `HERO_VECTOR_ENABLED` / `HERO_VECTOR_DSN` | 空 | `pgvector`/`postgres`/`auto` 时复用 checkpoint DSN，否则内存回退 |
| `HERO_VECTOR_DIM` | `32` | 8–2048，越界告警后回落默认 |
| `HERO_EMBED_PROVIDER` | `offline` | `openai` / `sentence-transformers` / `offline` |
| `HERO_OTEL_MODE` | `disabled` | `disabled`/`shared`/`private` |
| `HERO_TRACE_*` | `50000/500` | `TOOL_RESULT_OFFLOAD/TEXT_OFFLOAD/PREVIEW` |
| `FRONTEND_DIST` | 自动探测 | 显式设置即**唯一来源**：路径不存在时 fail-closed，**不回落** `/app/frontend/dist` 等默认候选（`server.py:2814`）。无产物时 `/` 与 SPA 路由返回 503 + `X-Frontend: missing|index-missing` + `Retry-After: 30` |

全仓仅 `src/hero_quant/config/settings.py` 允许 `os.getenv`，CI 门禁 `tests/test_config.py:80` 校验。

> **计费边界**：API 层没有任何 billing 路由，不存在"返回 200 的假成功面"；无 DSN 时 `BillingService` 直接抛 `BillingUnavailable`，内存模式必须显式 `HERO_BILLING_ALLOW_MEMORY=1` 且响应带 `degraded:true`。
> **合成模式边界**：`HERO_DATA_MODE=synthetic` 不是"离线可用"的同义词——它可跑通，但每一根 bar 都是合成序列，`provenance.source=synthetic` 必须被下游如实消费。

## API

- `GET /live` 存活、`GET /ready` 就绪、`GET /metrics` Prometheus、`GET /v1/query?q=...` 同步、`GET /v1/query/stream?q=...` SSE 流式
- `POST /v1/query/ticket` SSE 票据签发、`GET /v1/backtest/metrics.json` 回测指标、`GET /v1/backtest/positions.csv` 持仓、`GET /v1/backtest/tearsheet.html` 报告、`GET /v1/trace/events` 追踪事件、`GET /v1/risk/summary` 风险摘要（+ 别名 `/risk/summary`）、`WS /ws/trace` 与 `WS /ws/query` 进度推送
- `X-Request-ID` 贯穿日志（`TraceIdMiddleware` 透传 `x-request-id`/`x-trace-id` 并回写响应头），安全头 `X-Content-Type-Options: nosniff` + `X-Frame-Options: DENY`（`SecurityHeadersMiddleware`），审计 `api.audit` 结构化 tool 调用，PII 经 `PII_ENCRYPTION_KEY` Fernet 加密（缺失回退掩码）
- `POST /v1/query` 接受 `{query, stream:true}`（前端 `Chat.tsx:29` 已接 SSE + tool 轨迹水位）

示例：

```bash
curl "http://127.0.0.1:8899/v1/query?q=回测 600519.SH 近一月等权"
curl -N "http://127.0.0.1:8899/v1/query/stream?q=对比 茅台 vs 五粮液 近3月"
```

## 开发

```bash
ruff check src
black src
pytest -q --cov=src/hero_quant --cov-fail-under=50  # 以 pyproject fail_under=50 为准
npm --prefix frontend run test:run
```

**测试口径（唯一可信来源是本机实跑）**：硬门是 `pyproject.toml:67` 的 `fail_under = 50`，`.github/workflows/test.yml:53` 与 `ci.yml:103` 同值。2026-10-28 基线：**2767 passed / 47 skipped / 覆盖率 72.04%**（`pytest -q --cov=src`）。47 个 skip 里绝大多数是“无真 PG 时”的守卫（有 PG 时只剩 1 个 Windows-only skip），所以看到跳过数变化先看环境，不要先怀疑用例。通过数随改动漂移，引用前请重跑，不要照抄本文。前端路由用例自造临时 `dist` 来区分“有产物 200 / 无产物 503”，**不依赖** CI 先 `npm run build`。

供应链：`requirements-lock.txt` 为 `uv pip compile --generate-hashes` 产物（真 sha256 非占位），`Dockerfile:32 pip install --require-hashes`，CI 校验 `pip-audit + gitleaks`（已接入真 hash 全量）。

## 路线图

- [x] 数据双源 + provenance/单位/1% 校验
- [x] 回测 PIT/多标/tearsheet
- [x] AgentLoop 10 控制点 + LangGraph 双路由
- [ ] Memory 多租户 namespace + Governance 跨段校验
- [x] Checkpoint 接线（显式 `HERO_CHECKPOINT_DSN` → 真 PG saver，`agent/loop.py` 每轮迭代落一次；无 DSN → `memory`，DSN 不可达 → emulated 桩，`/ready` 如实标 degraded；宿主机 `100.114.54.26`、compose 内 `llm-pg`）
- [x] `requirements-lock.txt` 真 hash 全量（已 `uv pip compile --generate-hashes` 非占位）

## 合规与免责

> **非投资建议**：本项目仅供研究与教育参考，输出不构成任何投资建议（详见 `docs/COMPLIANCE.md`）。

- **投资免责**：LLM 存在幻觉与时效局限，回测收益不预示未来表现；交易决策由用户自担风险。
- **数据许可**：行情以 `tencent/yahoo` 为主，`tushare/akshare/yfinance/ccxt` 需自备许可并遵守其条款；全链路 `provenance{source,unit}` 可追溯（A股 `board_lots`/美股 `shares`）。
- **风险揭示**：证券投资涉及市场、流动性与系统风险，可能导致本金损失；单机部署、网络/模型抖动可能导致延迟不一致，请结合 `/metrics` 与日志巡检。

## 致谢

- [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 8 大模式与 8 月正确性修复清单
- 绿地实现，公式/文案自写，接口对齐 Vibe 语义

## 许可证

MIT
