# SLO 与错误预算（PR-144 骨架）

> **状态：口径提案 + 计算规则就绪，数值未经产品确认、未生成过报告。**
> 目标值、排除项、冻结阈值都标了 `待确认`；没跑过一次月度 SLO 报告之前，
> 这份文档只是"把口径写清楚"，不构成对外承诺。
>
> **硬前提（不听这条，下面所有数都不可信）**：SLI/错误预算规则用 **30 天窗口**，
> 因此 `HERO_PROM_RETENTION` 必须 **≥ 35d**。compose 与该 env 模板的默认值已改为 35d；
> 调小前先确认这台机器是否需要评估 SLO。保留期不足时规则产出的是**偏小甚至空的值**——
> 看起来像 SLO 很好，实际是窗口不够。`preflight.sh` 会在 <30d 时打印 WARN。

## 1. 目标（提案，待确认）

| SLI | 口径 | 目标（提案） | 窗口 |
|---|---|---|---|
| 可用性 | `1 - 错误率`，错误率 = `hero_quant_api_errors_total` / `hero_quant_requests_total` | 99% | 30 天滚动 |
| 延迟 | 各端点 p95（秒） | p95 < 5s | 5 分钟滚动（**30d 汇总未实现**，见 §2 口径偏差） |
| 降级可见性 | 响应带 `degraded:true` 的比例 | 仅观测，暂不设目标 | — |

排除项（**待确认**，先写清楚再谈数字）：

- 计划内维护窗口（发布/蓝绿切换期间的短暂 503）——是否计入，需要产品定。
- 依赖方故障（LLM provider 5xx/429）：从用户视角是失败，**倾向计入**；
  但响应若已显式 `degraded:true + reason`，是否单列待定。
- 压测/演练流量：应当排除（用独立 header/租户标记，本期还没做标记手段，
  所以演练期间的 SLI 会失真——演练请在台账里注明时间段）。

## 2. 计算规则

规则已落地在 `deploy/monitoring/slo.yml`：

| 记录规则 | 含义 |
|---|---|
| `heroquant:sli_availability:ratio5m` | 5 分钟窗口可用性 |
| `heroquant:sli_latency_p95:5m` | 各端点 p95（秒） |
| `heroquant:requests:increase30d` / `errors:increase30d` | 30 天窗口分子分母 |
| `heroquant:error_rate:ratio30d` | 30 天错误率 |
| `heroquant:error_budget_consumed:ratio30d` | 预算消耗比例（SLO 99% ⇒ 1% 为满额） |
| `heroquant:error_budget_remaining:ratio30d` | 剩余预算，1=没动，0=耗尽 |

**硬前提**：这些规则用 30 天窗口。生产 compose（`deploy/compose/docker-compose.prod.yml:190`）
与 `deploy/env/prod.env.example:36` 的 `HERO_PROM_RETENTION` 默认是 **35d**；但
`staging.env.example` / `dev.env.example` 是 **3d / 1d**——按它们起 Prometheus 时规则产出的是
**偏小甚至空的值**，看起来像"SLO 很好"，实际是数据窗口不够。要真算月度 SLO：
`HERO_PROM_RETENTION=35d`（并确认备份盘/数据盘容量）。`preflight.sh`（`scripts/deploy/preflight.sh:107-125`）
在 <30d 时打 WARN，但**不阻断**。

### 口径偏差（2026-10-31 复核 src 后补，读数字前必看）

1. **可用性分子不是纯请求错误**。`hero_quant_api_errors_total` 与请求计数器同一套写法，
   但 `api/server.py` 还用它计：`/ready` 探针（`endpoint="ready"`，:1319/:1351/:1836）、
   后台事件（`endpoint="checkpoint"/"warmup"/"otel"`，:135/:949/:1153）、以及"已显式降级但
   HTTP 成功"的响应（`error_code="degraded_response"`，:2193/:2622）。所以这个可用性 SLI 是
   **偏悲观的下界**：它不会虚高（不会把故障算成正常），但会把降级成功算成错误。
   要严格口径得在 src 另拆一个只记请求失败的计数器（本次监控轮不动 src）。
2. **延迟只有 5 分钟记录规则**（`heroquant:sli_latency_p95:5m`），没有 30 天 p95 聚合。
   月度报告里只能写"采样时点的 5m p95"，**不是月度 p95**；要月度 p95 得另加 recording rule
   （并用直方图分位做 30d 聚合，成本高、失真大，本期不做）。
3. **30d 窗口依赖 `increase()` 外推**：Prometheus 重启/抓取中断会在 `increase` 上留下缺口，
   数值偏小。看数字前先确认目标连续在线、保留期 ≥35d。

## 3. 错误预算策略

| 剩余预算 | 动作（提案） |
|---|---|
| > 50% | 正常发布 |
| 25%–50% | 正常发布，发布后加强观测 |
| 0–25% | 只发修复类与回滚，暂停功能发布 |
| = 0 | **冻结非修复类发布**（告警 `HeroQuantErrorBudgetExhausted` 已接，severity=critical） |

冻结的解冻条件：连续 24 小时错误率回到预算内，或产品/负责人书面例外。

## 4. 月度报告模板（未生成过）

| 月份 | 请求数 | 错误数 | 错误率 | 可用性 | 预算消耗 | p95（分端点） | 主要错误来源 | 结论 |
|---|---|---|---|---|---|---|---|---|
| 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |

生成方式（本期**没有**自动生成脚本，PR-313 才做）：按上表从 Prometheus 读数，
`heroquant:*` 记录规则已把查询收敛成一行一个指标，抄数即可，不要手算比率。

## 5. 没做的事

- 没有自动生成报告的脚本（PR-313）。
- 没有演练/压测流量标记，SLO 数据在演练窗口内会失真（已在 §1 注明）。
- 没有 Alertmanager 投递，预算冻结告警目前只在 Prometheus Alerts 页可见。
