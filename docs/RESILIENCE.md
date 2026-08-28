# 弹性与灾备 · RESILIENCE

> 目标：在 LLM、行情、存储三类依赖抖动时保持可用、可观测与可恢复。

## 1. LLM 多 Provider Fallback

- **顺序**：`openai → deepseek → anthropic`（由 `HERO_LLM_PROVIDER` 与 `HERO_API_KEY` 驱动，见 `src/hero_quant/config/settings.py`）。
- **策略**：主 provider 失败（限流/超时/5xx）自动切换下一家；每级重试带退避与预算熔断（`agent/policies` Retry•BudgetBreaker）。
- **离线兼容**：未配置密钥或依赖缺失时走 `synthetic` 占位，不阻塞回测与数据链路；`/metrics` 暴露 `llm_fallback_total`（后续）便于告警。
- **引用**：`src/hero_quant/llm/*`、`src/hero_quant/agent/policies/*`。

## 2. 行情数据源熔断与告警

- **CircuitBreaker**：`src/hero_quant/telemetry/circuit.py` 双桶熔断（失败率/慢调用率 ≥50% 触发，`slow_threshold=30s`，`OPEN 30s` 后 `HALF_OPEN` 探测 5 次）。
- **行为**：`CLOSED → OPEN (30s 拒流) → HALF_OPEN → CLOSED`；窗口 `60s` 滑动；`allow()`/`is_open()` 由调用方在请求前后探查。
- **告警指标**：
  - `circuit_state` (Gauge 0=closed/1=half_open/2=open)
  - `data_source_circuit_open_total{source}` (Counter) — 每次 `OPEN` 递增，`source` 标签区分 `tencent/yahoo/tushare/akshare/ccxt` 等，按源告警。
- **使用**：数据网关在 `get_market_data` 前 `allow()`，失败/慢调用 `record_failure(duration)`，成功 `record_success(duration)`。

## 3. 限流

- **双桶限流** `DualBucketRateLimiter`：`burst + sustained` 双 token bucket，原子获取；默认 `capacity=10, refill=5/s, burst=20/10/s`。
- **用途**：保护上游行情与 LLM，避免突发打满配额。

## 4. 备份与恢复

- **Postgres**：`docker-compose.yml` 中 `postgres:15`，数据卷 `pgdata`，建议 nightly `pg_dump` + 异地对象存储；`checkpoint/postgres.py` 支持 `memory://` 回退（PG 不可达时）与 `expires_at TTL 7d`。
- **运行产物**：`runs/` 下 `positions.csv/fills.csv/metrics.json/tearsheet.html` 挂载 `hero-runs` 卷；`governance/ledger` 按 `0600 + fsync` 落盘，支持 `verify()`。
- **前端与配置**：`frontend/dist` 随镜像构建；`.env` 与 `requirements-lock.txt`（hash 锁定）纳入版本控制与备份。
- **灾备演练**：定期验证 `pg_isready`、卷快照恢复、合成数据离线回测三条路径。

## 5. 观测与联动

- **日志**：`structlog JSON + X-Request-ID` 贯穿；熔断/限流/LLM fallback 均结构化落盘。
- **指标**：`/metrics` 暴露 Prometheus 指标（circuit、限流、LLM）；建议阈值告警 `data_source_circuit_open_total > 0` 且 `circuit_state==2` 持续 1m。
- **探活**：`telemetry/heartbeat` + `checkpoint/temporal` 15s 心跳侧车；Temporal 不可用时调度走 `scheduled fallback`（`temporal unavailable -> scheduled fallback`）。

## 6. 边界

- 当前备份为单机 nightly 策略，未做跨可用区多活；多租户与跨段校验在路线图中。
