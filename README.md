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
cp .env.example .env  # 必须填 HERO_PG_USER + HERO_PG_PASSWORD，否则 DSN 渲染成空口令、/ready 如实 503（详见「Docker 单机部署」）
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

前置条件：宿主上的 llm-pg / llm-redis 已在跑（它们由别的 project 创建，不归本
compose 管，也不会被 `docker compose down` 停掉）。新克隆只需下面两条命令。

```bash
# 0. 必做：.env 从示例复制并填 PG 口令（不填 → DSN 渲染成空口令 → /ready 如实 503）
cp .env.example .env
# 编辑 .env，至少补齐这两项（缺 HERO_PG_PASSWORD → DSN 口令段为空 → /ready 如实 503）：
#   HERO_PG_USER=<llm-pg 容器内 $POSTGRES_USER，不是 postgres>
#   HERO_PG_PASSWORD=<真实口令，只写 .env，永不进仓库>
# 主机段 .env.example 已预置为 100.114.54.26（本机 tailscale IP），一般不用改。

# 1. 起服务（一条命令；不需要先跑 scripts/init/setup-shared-network.sh —— 该脚本已 deprecated）
docker compose up --build
# http://127.0.0.1:8899  前后端同源（api 静态托管 frontend/dist）
# 数据落盘：named volumes hero-runs / hero-sessions / hero-home
# 硬化：cap_drop ALL + SETUID/SETGID(供沙箱) + no-new-privileges + read_only + tmpfs /tmp

# 仅构建镜像（单容器运行时需自行保证 PG/Redis 可达；建议直接用 compose）
# 版本以 pyproject.toml 为唯一来源（PR-181），镜像 tag 自定；需要 OCI 版本标签时：
docker build --build-arg HERO_QUANT_VERSION="$(python -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')" -t hero-quant:local .
```

生产单机（Caddy/TLS、systemd、备份/PITR、蓝绿发布）见 [`deploy/README.md`](deploy/README.md)；
生产编排入口是 `deploy/compose/docker-compose.prod.yml`，与上面的开发用 `docker-compose.yml` 相互独立。

> **主机名口径（2026-10-28 决策 D-1：宿主与容器统一，不再分叉）**
> - **宿主机直跑**（`uvicorn hero_quant.api.server:app --port 8899`）与 **compose 容器内**：PG/Redis **都写** `100.114.54.26`。容器经默认 bridge NAT 出网可达该 IP（已实测 5432/6379 通）。
> - `100.114.54.26` 是本机的 **tailscale IP**：换机器或该 IP 变了，要同步改 `.env` 的 `HERO_PG_HOST` / `HERO_CHECKPOINT_DSN` / `HERO_BILLING_DSN` / `HERO_VECTOR_DSN` / `HERO_REDIS_DSN`，以及 `docker-compose.yml` 中 `hero-quant.environment` 的对应项。
> - `127.0.0.1` / `localhost` 的 `5432`、`6379` **不通**——llm-pg / llm-redis 只绑 tailscale IP。
> - `llm-pg` 的超级用户名**不是** `postgres`，取容器内 `$POSTGRES_USER`。
> - 外部 PG 首次使用需先建库 `heroquant` 并执行 `scripts/init/001_schema.sql`（`local-stack` profile 由 initdb 自动执行；复用 llm-pg 时手动执行一次）。
> - 已废弃、仅保留供回滚：外部网络 `heroquant-shared` 与 `scripts/init/setup-shared-network.sh`（默认执行即 skip）。
> - `--profile local-stack` 会另起 postgres/redis/temporal，此时必须在 `.env` 显式把主机名改成 `postgres`/`redis`——compose 的 `environment` 优先级高于 `env_file`，无法按 profile 条件覆盖。

### /ready 与容器健康

- `/ready` 聚合探测 PG（checkpoint + billing）与 Redis，全程 <5s 有界；**缺 checkpoint DSN 或库不可达即 503** + `degraded:true` + `reasons`（这是诚实降级，不是 bug）。
- 缺 `COHERE_API_KEY` 默认只 warning；只有 `HERO_READY_COHERE_FATAL=true` 才判致命。
- `Dockerfile:108-109` 的 `HEALTHCHECK` 已从 `/live` 改为 `/ready`——容器 `healthy` 现在代表**依赖可用**，不再只是进程活着。
- 前端唯一可信产物是 `frontend/dist`：dist 陈旧（缺新路由 chunk）必须 `npm run build`；**dist 目录或 `index.html` 缺失时返回 503**（`X-Frontend: missing|index-missing` + `Retry-After: 30`，`server.py:2851-2863`），不再用 200 占位页让编排层误判健康。JSON 请求拿 503 `degraded` body；缺失的静态资源（`.js/.css/.map/.png/.svg/.woff2` 等已知扩展名）仍按 404 返回，不会伪装成页面。

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
| `HERO_PG_HOST` | `100.114.54.26` | 宿主与容器统一用本机 tailscale IP（决策 D-1）；换机器/IP 变了要改。显式 `HERO_*_DSN` 优先 |
| `HERO_PG_USER` / `HERO_PG_PASSWORD` | 空 | 供 compose 的 DSN 变量插值；llm-pg 超级用户名**不是** `postgres`，取容器内 `$POSTGRES_USER`；口令只写 `.env` |
| `HERO_PG_DSN` | 空 | checkpoint DSN 的 legacy 别名（显式 `HERO_CHECKPOINT_DSN` 优先） |
| `HERO_CHECKPOINT_DSN` | `postgresql://100.114.54.26:5432/heroquant`（无口令） | 显式优先；compose 内渲染为 `postgresql://<user>:<password>@100.114.54.26:5432/heroquant`。**`.env` 不填 `HERO_PG_PASSWORD` 时口令段为空**，`/ready` 如实 503 |
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
| `HERO_AUTH_MODE` | `enforce` | `off`=迁移期关闭强制鉴权；`enforce`=强制 Bearer（非法值也按 `enforce`）。`auth_enabled()` 还要求 issuer + audience 同时存在，缺配置时开发环境不会被打穿 |
| `HERO_OIDC_ISSUER` | 空 | OIDC issuer，形如 `https://sso.example.com/realms/hero`（Keycloak realm） |
| `HERO_OIDC_AUDIENCE` | 空 | 期望 `aud`；与 issuer 同为该开关键，缺一即不强制鉴权 |
| `HERO_OIDC_JWKS_URL` | 空 | 留空按 Keycloak 约定推导 `{issuer}/protocol/openid-connect/certs` |
| `HERO_OIDC_ALGORITHMS` | `RS256` | 只接受非对称算法；`none` / HS256 一律拒绝 |
| `HERO_OIDC_LEEWAY_SECONDS` | `60` | 校验 `exp/nbf/iat` 的时钟偏移容忍 |
| `HERO_OIDC_JWKS_TTL_SECONDS` | `300` | JWKS 缓存 TTL；取签失败时不静默放行 |
| `HERO_TENANT_REQUESTS_PER_MINUTE` | `60` | 租户级 Redis 滑动窗口限流；超限 429 |
| `HERO_TENANT_DAILY_TOKEN_BUDGET` | `2000000` | 租户日 token 预算；`<=0` 表示不限 token |

全仓仅 `src/hero_quant/config/settings.py` 允许 `os.getenv`，CI 门禁 `tests/test_config.py:80` 校验。

> **身份接线状态（W1，已接线）**：`api/server.py:534` 的 `_wire_auth()` 在 app 创建时挂载 `AuthContextMiddleware` + 统一 `AuthError` 映射；判据是 `auth_enabled()`（模式非 off **且** issuer/audience 齐备），因此**未配 OIDC 的环境不会被打穿**。挂载后：`/v1/query`、`/v1/query/stream`、`/v1/query/ticket` 需 analyst，`/v1/sessions*` 需 viewer；无 token 401 + `WWW-Authenticate`，错签名 401，配置故障 503（不伪装成 401 诱导重登）。`/health` `/ready` `/metrics` `/docs` 保持公开。验签失败一律 fail-closed：OIDC 配了但不全，进程直接启动失败。
> **租户边界口径**：已认证请求的 tenant 来自 JWT claim，贯穿 checkpoint thread（`_checkpoint_thread_for_request`）与会话接口；**会话接口未绑定租户时 503，绝不回退 `default`**。kill switch（租户/模型/工具）命中或 Redis 不可用同样 503，不静默放行。
> **配额口径（PR-105 速率部分）**：`/v1/query`、`/v1/query/ticket`、`/v1/query/stream` 已挂租户滑动窗口限流，超限 429 + `Retry-After`；**Redis 不可用是 503（`auth_config_error`）而非 429**，用户超限与服务端故障不混为一谈。日 token 预算（`HERO_TENANT_DAILY_TOKEN_BUDGET`）的记账尚未接线——需要先把 LLM gateway 接进实际调用链拿到 usage，在此之前该变量只影响策略对象、不产生扣减。

> **计费边界**：API 层没有任何 billing 路由，不存在"返回 200 的假成功面"；无 DSN 时 `BillingService` 直接抛 `BillingUnavailable`，内存模式必须显式 `HERO_BILLING_ALLOW_MEMORY=1` 且响应带 `degraded:true`。
> **合成模式边界**：`HERO_DATA_MODE=synthetic` 不是"离线可用"的同义词——它可跑通，但每一根 bar 都是合成序列，`provenance.source=synthetic` 必须被下游如实消费。

## API

- `GET /live` 存活、`GET /ready` 就绪、`GET /metrics` Prometheus、`GET /v1/query?q=...` 同步、`GET /v1/query/stream?q=...` SSE 流式
- `POST /v1/query/ticket` SSE 票据签发、`GET /v1/backtest/metrics.json` 回测指标、`GET /v1/backtest/positions.csv` 持仓、`GET /v1/backtest/tearsheet.html` 报告、`GET /v1/trace/events` 追踪事件、`GET /v1/risk/summary` 风险摘要（+ 别名 `/risk/summary`）、`WS /ws/trace` 与 `WS /ws/query` 进度推送
- `GET /v1/sessions`（列表）、`GET /v1/sessions/{id}`（详情）、`POST /v1/sessions/{id}/resume?after_id=&limit=`（断线/重启续跑，返回增量消息 + `last_event_id` 游标）：全部按当前租户取数，未绑定租户 503；不存在 404、存储不可用 503、入参非法 400
- **降级口径**：`_FakeLLM` / checkpoint emulated / LLM gateway failover 三种降级都会落进响应的 `degraded:true` + `degraded_reason`（多来源以 `; ` 拼接），前端必须横幅提示；gateway 全挂由 `_FakeLLM` 路径负责，failover 只报 `llm_gateway:*`
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

**测试口径（唯一可信来源是本机实跑）**：硬门是 `pyproject.toml:67` 的 `fail_under = 50`，`.github/workflows/test.yml:53` 与 `ci.yml:103` 同值。2026-10-28 基线：**2767 passed / 49 skipped / 覆盖率 72.15%**（`pytest -q --cov=src`）。49 个 skip 里绝大多数是“无真 PG 时”的守卫（有 PG 时只剩 1 个 Windows-only skip），所以看到跳过数变化先看环境，不要先怀疑用例。通过数随改动漂移，引用前请重跑，不要照抄本文。前端路由用例用 `tmp_path` 自造 dist 做**三态断言**（有 `dist` 断 200 HTML；`dist` 缺失断 503 + `X-Frontend: missing`；`index.html` 缺失断 503 + `X-Frontend: index-missing`），并通过 reload 锁定分支，**不依赖** CI 先 `npm run build`、也不随本机是否构建过而漂移。

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
