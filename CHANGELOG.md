# Changelog

All notable changes to hero-quant are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-08-28

### Supplement Wave5 — docs honesty + backtest detail + monitor + coverage

> 收口 2026-08-28 supplement 审计 12 项遗漏中的 P0/P1 细节：D1 文档诚信 + B1 sma/on_bar + C1/C2 前端去 mock + D2/E1 ingest/coverage。基于 `7559422+f639a78+e79dfc2` (302 passed, 48.99% cov) 增量。

### Added
- **Docs honesty (D1)**: `README:56,100` `HERO_DATA_MODE` `synthetic→live` 与 `Settings` 一致；`README:137` `memory://→PG (fallback memory)`；`README:129` `hash占位→real hashes` 真 sha256；`docs/retrieval_eval.md` 追加 32/128/768 维度 ablation stub。
- **Backtest detail (B1)**: `engine.py` `sma_crossover` 熊市返回 0 权重（`bear→0`）；`BacktestEngine.run` 串联 `on_bar` `aligned_price` 至 `equity` 计算（`cum*(1+aligned_ret)`，保留 close 兼容）；新增 `tests/test_golden_backtest.py` 固定 5 日 oracle 对拍 `metrics.sharpe`。
- **Monitor + Frontend (C1/C2)**: `App.tsx` 新增 `/monitor` 路由与导航；`Monitor.tsx` 移除 `Math.random<0.3` 改 `fetch /v1/trace/events` SSE 真流；`Live.tsx` 移除 `0.28` mock 定时；`Research.tsx` 热力图由 `metrics.monthly` / `metrics.monthly_returns` 真算驱动。
- **Coverage + Ingest (D2/E1)**: `ingest.py` key 生成改 `hashlib.sha256(piece.encode()).hexdigest()[:8]` 确定性；`loop` 注入 `skills_digest` 并断言 prompt 含 digest；`tests/test_backtest_engine.py` 增厚多资产/转仓用例提覆盖率；抬 `cov fail_under 48→50`。

### Changed
- `pyproject.toml` / `.github/workflows/test.yml` `fail_under` 48→50。
- `frontend/src/App.tsx` 导航 5→6（含 Monitor）。
- `requirements-lock.txt` 状态描述去“占位”，改为真 hash。

### Verification
- `pytest -q --cov-fail-under=50` PASS (≥50%)
- `ruff check src` 0 error
- `npm --prefix frontend run test:run` 5+ files PASS

## [0.2.0] - 2026-08-21

### Release harness polish — Wave G (deploy-ready)

> Docker 单机可部署 + 173 tests green + wall-time/metrics/guardrails 收口。

### Added
- **Agent & Graph (Wave A-B4 / maturity3)**: `AgentLoop` token estimate `//4`, wrap-up nudge at `0.8*max_iterations`, batch grounding freeze, VCR `llm_usage` record/replay (`loop trace + llm_usage.json`), LangGraph `Send` fanout parallel, pros/cons verify, BM25 router `K1=1.5 B=0.75`, context vector folding 80% threshold, parallel readonly `ThreadPool(8)` pool.
- **Data (Wave B)**: `safe_ticker_component` allowlist `^[A-Za-z0-9._\-\^=+]+$`, loader trait contract, Yahoo/AKShare/CCXT multi-market loaders, Provenance `{source,unit}` + 1% cross-source block, synthetic fallback fidelity.
- **Backtest (Wave B3)**: PIT strict `weights_on ≤ price_date`, multi-asset / multi-engine (event-driven + vectorized/Polars), `positions.csv/fills.csv/metrics.json/tearsheet.html` artifacts, monthly ME heatmap + drawdown Top3, quality gates M1-M4, bench batch + regional `benchmark_map`.
- **Quantlib**: Rust crate `crates/quantlib` stub (`sma/ema/rsi/bollinger/macd/max_drawdown`) + Py fallback + parity `perf_gate`, Polars base, BS pricing.
- **Telemetry & Resilience (Wave C)**: OTel 3-mode `disabled|shared|private` + BatchLogRecordProcessor, Prometheus `hero_quant_requests_total` + `http_request_duration_seconds` histogram + `circuit_state` gauge, Trace hard thresholds `TOOL_RESULT_OFFLOAD=50k` / `TEXT_OFFLOAD=50k` / `TOOL_RESULT_LIMIT=10k` with `tmp→fsync→link` sidecar, `HeartbeatTimer` 0.5s min 4-layer + `CircuitBreaker` dual-bucket `50%/30s/open30s/half5s`, `RetryPolicy` exponential+jitter + `BudgetBreaker daily_limit`, `/metrics` + SSE `StreamingResponse(event_generator)` true incremental.
- **Governance & Security (Wave C5-C7)**: Ledger `seq/prev_hash/record_hash 0600+fsync` + `verify()` cross-segment recompute + namespace `tenant:thread` isolation, `DedupStore` PG `INSERT ... ON CONFLICT` + `FOR UPDATE` with memory fallback, credentials `0600 + REF_PATTERN` hot reload, HMAC + Host allowlist (`VIBE_TRADING_TRUST_DOCKER_LOOPBACK`), AST guard `socket/subprocess/os.system/ctypes/eval` ban, CSP `default-src 'self'` + `X-Frame-Options: DENY`, redaction `ARGUMENTS_SINK/RESULT_SINK` auto-wired in `trace/ledger`.
- **Memory**: File + FTS5 + trigram + 30s `content_hash` sliding-window cross-key dedup, `namespace` FTS/LIKE/file isolation, lifecycle `hierarchy` + `decay Ebbinghaus 14d` + GC rotation keep-pending.
- **MCP & Tooling**: 15+ `@tool` registry deterministic `is_concurrency_safe` audit, `mcp/router` TopK5 vector router + hybrid recall, `mcp/server` wall-time metrics.
- **Frontend & E2E (Wave D)**: 5 routes `Dashboard/Research/Backtest/Live/Risk` + `Live` SSE/WS `<200ms on_tick` + `Reconcile` + `Billing` RLS + `Scheduled` Temporal Cron 5 playbooks, `Tearsheet` ECharts ME + drawdown, Playwright shadow reconciliation.
- **Supply chain & CI (Wave A3-A4)**: `requirements-lock.txt --generate-hashes` real hashes + `--require-hashes` builder, `ruff` 0 error, `pip-audit` + `gitleaks`, CI matrix `ubuntu` + 3-job `hash-lock/lint/test`.
- **Docs & Plans**: `docs/plans/2026-08-21-hero-quant-deploy-ready.md` Wave G release harness, `2026-08-21-hero-quant-maturity3-tasks.md` 7-dim ≥3.0 roadmap, TradingAgents deep-dive annex.

### Changed
- Version bump `0.1.0 → 0.2.0` across `pyproject.toml`, `src/hero_quant/__init__.py`, `frontend/package.json`, `crates/quantlib/{Cargo.toml,src/lib.rs}`, `Dockerfile` OCI label, `frontend/src/App.tsx`.
- `README.md` Docker tags `hero-quant:0.2.0`, `tests/test_bootstrap.py` version assert updated.
- Wall-time budgets tightened, Prometheus histogram buckets stabilized, guardrails centralized in `src/hero_quant/metrics/` and `config/limits.py`.

### Fixed
- `store.py` namespace leakage in FTS fallback, `loop.py` batch snapshot isolation, `quantlib` decimal/Bollinger edge cases, `redaction` content passthrough, `dedup` PG conflict races, `otel` offline no-op, `sandbox` `vibe-sandbox` uid 10001 contract.

### Security
- Path traversal rejected via `safe_ticker_component`, redaction before persistence, ledger tamper detection, `0600` file modes, `read_only` + `cap_drop ALL` + `no-new-privileges` runtime.

### Verification
- `pytest -q` — 173 passed (wall-time/circuit/metrics guardrails enforced)
- `ruff check` — 0 error
- `docker compose config` — valid
- `pip install --require-hashes -r requirements-lock.txt` dry-run — ok

## [0.1.0] - 2026-08-20

### Added
- Initial thin-skeleton COE-quant kernel (Vibe-Trading 8 patterns, Docker 3-stage, `api/server.py:100` StaticFiles, `.dockerignore`, 44 tests green).

[0.3.0]: https://github.com/your-org/hero-quant/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/your-org/hero-quant/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/your-org/hero-quant/releases/tag/v0.1.0
