# Telemetry 模块深挖（structlog + 熔断/限流 + OTel 离线安全）

> 依据：`docs/recon.md` §1（可观测）、`docs/issues.md` P2-1/P2-2/P2-4。行号为 2026-10-19 快照实测值。

## 1. 职责一句话

`src/hero_quant/telemetry/` 提供三件套：结构化日志（`structlog`，脱敏 schema）、进程内熔断与限流（`CircuitBreaker/TokenBucket/DualBucketRateLimiter`，prometheus 可选）、OTel 导出（`HERO_OTEL_MODE` 门控，SDK 优先、urllib 回退、durable 队列保底，全链路离线安全）。

## 2. 关键文件与行号

| 文件 | 行 | 内容 |
|---|---|---|
| `src/hero_quant/telemetry/otel.py:5` | 设计 docstring | “`HERO_OTEL_MODE` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境门控；离线安全（无 SDK/无网络静默）；SDK 优先 LoggerProvider+BatchLogRecordProcessor，缺失回退 urllib” |
| `src/hero_quant/telemetry/otel.py:17-20` | logger | `structlog` 可选依赖，缺失回退兼容垫片（kwargs 键值对）；`structlog.get_logger("telemetry.otel")(:20)` |
| `src/hero_quant/telemetry/otel.py:177-179` | 模式门 | `get_otel_mode()` 取 `HERO_OTEL_MODE`，默认 `disabled`；未知值 fail-closed 回 `disabled` |
| `src/hero_quant/telemetry/otel.py:203-238` | durable 队列 | `_durable_queue_path(:203)` / `durable_queue_depth(:235)`（审计保底深度探针） |
| `src/hero_quant/telemetry/otel.py:379-477` | 导出管线 | SDK 批量管线单例复用；`endpoint` 取 `OTEL_EXPORTER_OTLP_ENDPOINT(:386)`；`BatchLogRecordProcessor(:402,477)`；SDK 缺失/失败 → urllib 回退（`:424-426` “永不走 urllib…此时直接回退 urllib，保证审计不断流”；`:537` “SDK 路径失败仅告警，随后落 urllib 回退，不 early return 丢遥测”） |
| `src/hero_quant/telemetry/otel.py:541-567` | urllib 回退 | `urllib.request` 同步 POST JSON（`:544,560-561` 0.5s 超时）；失败落本地 durable 队列，不断审计，不抛错（`:564-567`；`logger.warning("otel urllib export failed", exc_info=True)`） |
| `src/hero_quant/telemetry/circuit.py:18-44` | prometheus 可选 | `Gauge/Counter` 缺失时 `except Exception` 回退（`:28,44` “prometheus_client not available”） |
| `src/hero_quant/telemetry/circuit.py:51-310` | 熔断/桶 | `CircuitBreaker(:51)`（`record_failure/record_success/record_slow/allow/is_closed/is_open/try_acquire`）；`TokenBucket(:317)`；`DualBucketRateLimiter(:389)` |
| `src/hero_quant/telemetry/heartbeat.py:29-86` | 心跳 | `_set_emitter/_get_emitter(:29,33)`、`temporal_heartbeat(:76)`、`get_temporal_heartbeat_details(:86)` |
| `src/hero_quant/telemetry/heartbeat.py:120-202` | 探针 | `_probe_downstream_status(:120)`、`probe_temporal_sidecar(:176)/_v2(:189)`、`sidecar_heartbeat_probe(:195)`；`mark_probe_downstream_down/clear_probe_override(:159,168)` |
| `src/hero_quant/telemetry/heartbeat.py:203-328` | 定时器 | `HeartbeatTimer(:203)`（`_run/probe/heartbeat`） |
| `src/hero_quant/telemetry/__init__.py:20` | 导出 | `SessionTelemetryCoordinator, durable_queue_depth, get_otel_mode` |
| `src/hero_quant/api/server.py:75` | 链路日志分级 | “链A异常升级 warning/error；链B（backtest/billing/checkpoint/risk/ready）异常 warning” |
| `src/hero_quant/api/server.py:888-889` | `/ready` 池 | `_READY_POOL = ThreadPoolExecutor(max_workers=4)`（D 线已落地全局复用池，治 P2-4“每次新建池”） |
| `src/hero_quant/api/server.py:896` | 就绪缓存 | readiness TTL 缓存（5s），避免每次 `/ready` 打 `SELECT 1` |
| `src/hero_quant/api/server.py:1008,1099` | 实探 | `SELECT 1` 带 2s 超时（recon 记 `server.py:760,843`，实测下移至此附近，属 P2-4 同块） |
| `src/hero_quant/api/server.py:1194-1245` | `/ready` 本体 | `@app.get("/ready")(:1194)`；docstring `:1196-1200` “聚合 degraded… checkpoint 缺配与 billing fail-closed 以 reasons 透出；整体 degraded 时 503 + degraded:true + reason”；`status="degraded"` 三分支（`:1229,1231,1234`）；`degraded = status != "ok"(:1244)` |
| `src/hero_quant/api/server.py:938-949,964-973` | 脱敏范例 | `dsn_host="***"` + `error_code="checkpoint_dsn_missing/billing_dsn_missing"`（D 线 structlog schema 先例） |
| `src/hero_quant/checkpoint/postgres.py:44` | 静默收口 | `_log_silent_handled`（统一 warning + error.code + 脱敏 dsn_host + exc_info，见 `:280-283` 调用） |
| `src/hero_quant/governance/dedup.py:26` | 治理观测 | `_dedup_observe(op, start, status)`（去重操作指标打点范例） |

## 3. 上游 / 下游

- 上游：`server.py` 全链路（`/ready` 探针、链A/链B异常分级日志、`/metrics` prometheus 暴露）+ `AgentLoop` 壁时间/预算事件 + `governance/dedup` 去重观测。
- 下游：stdout 结构化日志 → OTel collector（`deploy/` 仅 otel，见 recon §1）→ durable 本地队列（collector 不可达时保底）。

## 4. 降级语义（离线安全：宁可本地保底，不可抛错阻断业务）

| 场景 | 语义 | 依据 |
|---|---|---|
| OTel SDK 缺失 | 回退 urllib 直 POST | `otel.py:5,379-426` |
| 无 endpoint / 无网络 | 静默 + 落 durable 队列，不抛错 | `otel.py:5,564-567` |
| SDK 路径失败 | 仅告警，随后仍走 urllib（不 early return 丢遥测） | `otel.py:537` |
| `HERO_OTEL_MODE` 未知值 | fail-closed 回 `disabled` | `otel.py:177-179` |
| prometheus 缺失 | 回退空实现，熔断/限流逻辑照常 | `circuit.py:28,44` |
| `/ready` 探针超时 | 全局池 + 2s 超时 + TTL 缓存，超时记 fail-closed(memory) 而非 hang 住 | `server.py:888-896,1008,1099` |
| DSN 进日志 | 一律脱敏 `dsn_host="***"`，PII/API key 永不进日志 | `server.py:943,966`；AGENTS §7 |

## 5. 日志 schema 约定（D 线执行中，先例先行）

- 必备字段：`trace_id / ticket_id / dsn_host(脱敏) / error.code`（AGENTS §7；`checkpoint/postgres.py:280-283` 已示范 `checkpoint.default_dsn_failed + checkpoint_default_dsn_failed + exc_info + warning`）。
- 反模式：`server.py:2038,2057,2143,2147` 链B `logger.debug(f"...error={_e}")` f-string 插值（无 error_code、无脱敏）→ 待 D 线按链B分级提 `warning`（P2-1）。
- 反模式：`server.py:607-609` `except Exception … logger.debug("otel.export.failed") + pass`（P1-3 点名，宽捕获 + 静默）→ 收窄异常类型 + 指标计数（D 线）。

## 6. 已知问题挂钩

- P2-1/P2-2（`server.py` 30+ 静默、checkpoint 无结构化字段）：telemetry 是执行方——统一 `structlog` schema 的落地位置即本模块 + `checkpoint/postgres.py:44` 收口函数。归 D 线。
- P2-4（`/ready` 双探误报）：`server.py:888-896` 全局池 + 缓存已落地；剩余工作为区分 liveness/readiness（D 线）。
