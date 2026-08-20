# 真英雄量化 · hero-quant

<p align="center">
  <b>自然语言 → 行情 → 回测 → 报告</b> · 单机可部署的极简投研 Agent
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/FastAPI-0.104+-009688" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react" alt="React 19" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT" />
  <img src="https://img.shields.io/badge/Tests-44_passed-brightgreen" alt="tests" />
</p>

> 批判性借鉴 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 8 大工程模式（AgentLoop 状态机 / 上下文折叠 / Grounding 证据账本 / Tool 自动发现 / 行情 Fallback+Provenance / 回测引擎 / 治理 Hash 链 / 供应链契约），绿地重写核心业务，单进程 + Docker 单机可部署。

## 特性

- **投研闭环**：一句话完成 `600519.SH 近一月等权回测` → 自动拉行情 → PIT 校验 → 跑回测 → 出 `positions.csv/fills.csv/metrics.json/tearsheet.html` + 证据账本
- **数据保真**：`tencent`(A股 `board_lots`) / `yahoo`(美股 `shares`) 双源 + `16 源注册表`，`provenance{source,unit}` 全链路记录，跨源收盘价 1% 阈值告警
- **回测正确性**：PIT `weights_on ≤ price_date` 正逻辑、非正价格/混币种拒绝、按换手计费、多标权重、月度热力 tearsheet
- **Agent 可靠性**：10 控制点状态机（`max_iterations/token_limit/TRUNCATED/user_stop/Retry•BudgetBreaker/Tool审计/Grounding/Context折叠/Trace 50k侧车`），`LangGraph StateGraph(plan→execute→verify)` 研究团队可选（`use_graph=True`）
- **工具**：`@tool(parameters/output/is_concurrency_safe)` 语义化合约，`get_market_data / run_backtest / technical_indicators / quantlib_call` 等 15 核心工具，读 True 写 False 并发审计
- **Quantlib**：`sma/ema/rsi(Wilder EWM)/bollinger/macd/max_drawdown` 纯 pandas，已对齐 8-11 RSI 修复
- **可观测**：`structlog JSON + X-Request-ID + /metrics(prometheus)`，`TraceWriter` `tmp→fsync→link` 硬链侧车 + `_safe_sidecar_path`
- **治理**：`governance/ledger.py` `seq/prev_hash/record_hash 0600+fsync` 可 `verify()`，`dedup` 幂等表（单机 dict，预留 PG）
- **前端**：React 19 + Vite + Zustand + Tailwind，`Chat / Research / Settings` 三页，`Research` 接真实 `tearsheet`，`Chat` SSE 流式 + tool 轨迹

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
cp .env.example .env  # 若无则直接 export
export HERO_LLM_PROVIDER=openai
export HERO_API_KEY=sk-...
export HERO_DATA_MODE=synthetic   # synthetic | live  (live 才调 qt.gtimg.cn/yahoo)
export HERO_LLM_MODEL=gpt-4o-mini

# 3. 跑测试
pytest -q  # 44 passed

# 4. 启动
uvicorn hero_quant.api.server:app --host 0.0.0.0 --port 8899 --reload
# 或 python -m hero_quant.api.server
open http://127.0.0.1:8899          # 前端（若 frontend/dist 已 build）
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
docker compose up --build
# http://127.0.0.1:8899  前后端同源（api 静态托管 frontend/dist）
# 数据落盘：named volumes hero-runs / hero-sessions / hero-home
# 硬化：cap_drop ALL + SETUID/SETGID(供沙箱) + no-new-privileges + read_only + tmpfs /tmp

# 仅构建镜像
docker build -t hero-quant:0.1.0 .
docker run -p 127.0.0.1:8899:8899 --env-file .env hero-quant:0.1.0
```

`Dockerfile` 3 阶段：`node:22-slim` 前端构建 → `python:3.11-slim` builder(`venv+hash-lock`) → `runtime`(无编译器，`vibe:vibe-sandbox 10001` 非 root)。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `HERO_LLM_PROVIDER` | `openai` | `openai` / `deepseek` / `anthropic` 等 |
| `HERO_LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `HERO_API_KEY` | — | LLM 密钥 |
| `HERO_DATA_MODE` | `synthetic` | `synthetic` 本地合成（测试/离线）/`live` 调真实行情 |
| `HERO_DATA_MARKET` | `CN` | 默认市场 |
| `HERO_OTEL_MODE` | `disabled` | `disabled`/`shared`/`private` |
| `HERO_TRACE_*` | `50000/500` | `TOOL_RESULT_OFFLOAD/TEXT_OFFLOAD/PREVIEW` |
| `FRONTEND_DIST` | 自动探测 | 前端 dist 覆盖路径 |

全仓仅 `src/hero_quant/config/settings.py` 允许 `os.getenv`，CI 门禁 `tests/test_config.py:80` 校验。

## API

- `GET /live` 存活、`GET /ready` 就绪、`GET /metrics` Prometheus、`GET /v1/query?q=...` 同步、`GET /v1/query/stream?q=...` SSE 流式
- `X-Request-ID` 贯穿日志，`POST /v1/query` 接受 `{query, stream:true}`（前端 `Chat.tsx:29` 已接 SSE + tool 轨迹水位）

示例：

```bash
curl "http://127.0.0.1:8899/v1/query?q=回测 600519.SH 近一月等权"
curl -N "http://127.0.0.1:8899/v1/query/stream?q=对比 茅台 vs 五粮液 近3月"
```

## 开发

```bash
ruff check src
black src
pytest -q --cov=src/hero_quant --cov-fail-under=30  # core 30 起步
npm --prefix frontend run test:run
```

供应链：`requirements-lock.txt` 为 `uv pip compile --generate-hashes` 产物，`Dockerfile:32 pip install --require-hashes`，CI 校验 `pip-audit + gitleaks`（占位已接入）。

## 路线图

- [x] 数据双源 + provenance/单位/1% 校验
- [x] 回测 PIT/多标/tearsheet
- [x] AgentLoop 10 控制点 + LangGraph 双路由
- [ ] Memory 多租户 namespace + Governance 跨段校验
- [ ] Checkpoint 真 PG 落盘（当前 `memory://` 可跑）
- [ ] `requirements-lock.txt` 真 hash 全量再生（当前含占位）

## 致谢

- [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 8 大模式与 8 月正确性修复清单
- 绿地实现，公式/文案自写，接口对齐 Vibe 语义

## 许可证

MIT
