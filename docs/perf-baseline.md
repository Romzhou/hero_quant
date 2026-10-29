# 单机容量与演练台账（PR-302 / PR-303）

> **这张表是待填的台账，不是报告。** 所有数值列初始为 `待测`，
> 没跑过的场景不许写结果，也不许从别的项目抄数字——抄来的基线会让告警阈值失真。

## 1. 目标 Definition of Done

- 压测：回答"单机能扛多少并发/多少 SSE 长连接/多少回测并行"，并给出扩容阈值。
- 演练：每类注入过的故障都有记录（检测到降级的时间、恢复到 `/ready=200` 的时间）。
- 阈值：告警里的延迟/成本阈值必须**回填自本表**，不能一直用占位值。

## 2. 压测台账

前置：`scripts/perf/smoke.sh` 通过；有 LLM key（**无 key 时跑出来的延迟不算基线**，
`_FakeLLM` 会接管，响应带 `degraded:true`，脚本 `degraded_ratio` 指标会记录这一点）。

```bash
BASE_URL=https://quant.example.com scripts/perf/smoke.sh
k6 run scripts/perf/loadtest.js
```

| 场景 | VUs 峰值 | 时长 | p50 | p95 | p99 | 错误率 | degraded 比例 | 备注 |
|---|---|---|---|---|---|---|---|---|
| `health`（/live、/ready 探针） | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 给"纯净"参考，证明探针在负载下不假绿 |
| `query_sync`（/v1/query） | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 主链路，最容易被限流/超时 |
| `query_stream`（/v1/query/stream） | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | k6 只验证握手与首字节；帧延迟需用浏览器/e2e 观测 |
| `backtest`（/v1/backtest/metrics.json） | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 重算密集，关注 CPU/PG 连接数 |
| `risk`（/v1/risk/summary） | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 依赖回测产物 |

同期要记录的配套观测：PG 连接数、Redis 命中/淘汰、LLM 速率限制（429）、
容器 CPU/内存（`docker stats` 或 cadvisor）、磁盘 IO。没有 exporter 前用 `docker stats` 采样。

回填后把阈值写回 `deploy/monitoring/alerts.yml`（注释已标 `待首次基线后回填`）。

## 3. 故障演练台账

```bash
scripts/perf/fault-drill.sh --list
scripts/perf/fault-drill.sh --case redis-down --confirm
```

记录落 `${HERO_DRILL_LOG:-/var/lib/hero-quant/deploy/drills.jsonl}`，每条含
`detected_s`（注入到观测到降级）与 `recovered_s`（恢复到 `/ready=200`）。

| 故障 | 期望行为 | 检测耗时 | 恢复耗时 | 是否已演练 | 备注 |
|---|---|---|---|---|---|
| Redis 不可用 | `/ready` 转 503 并给出 reason；限流器 fail-closed；回测仍可跑 | 待测 | 待测 | 否 | 见 `docs/runbook.md` §3.2 |
| PG 重启 | `/ready` 短暂降级；checkpoint 机制自愈 | 待测 | 待测 | 否 | 注意 degrade + reason 必须显式 |
| 应用容器被 kill | compose `restart: unless-stopped` 自愈，`/ready` 回到 200 | 待测 | 待测 | 否 | 蓝绿下另一色可接流量 |
| 磁盘空间不足 | 备份/WAL 写入失败并告警 | 待测 | 待测 | 否 | 本期只提供只读 `disk-report`，不自动填盘 |
| LLM 超时 / 429 | gateway 切备或抛 `AllProvidersFailed`，**不退回假文本** | 待测 | 待测 | 否 | 需 provider 侧配合，脚本未提供 case |
| TLS 证书到期 | smoke 脚本能读出到期日并预警 | 待测 | n/a | 否 | `cert-expiry` 只读 case |

## 4. 没做的事（不要假装做了）

- 没有 Prometheus/Grafana 之外的黑盒探测；公网可达性靠 `smoke.sh` 顶着。
- 没有用 `promtool` 校验过告警规则（本机无 promtool），首次接线必须补这一步。
- 没有任何一次真实压测或演练数据；表里的 `待测` 就是当前状态。
