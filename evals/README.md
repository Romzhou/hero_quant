# P线 Agent 评测集

30+ 条自然语言投研查询，断言“该调哪个工具 / 该产出什么结构 / 哪些数字必须出现”。

## 一键重跑

```bash
python evals/run.py --mode fake   # 无需 key，FakeLLM + AgentLoop，基线
python evals/run.py --mode real   # 需真实 LLM 后端；缺 SDK/key 时自动跳过并注明
```

## 文件

- `dataset.jsonl` — 38 条用例（JSONL，每行一个 case）。
  字段：`id / category / query / expected_tools / expected_structure / must_contain / fake_expect`。
  分类：A 市场行情(6) / B 基本面(5) / C 指标因子(6) / D 相关组合(4) /
  E 回测(8) / F 边界降级(9)。
- `run.py` — 评估入口，三层断言：
  1. `expected_tools` ⊆ `tools/registry.TOOL_REGISTRY`（防幻觉工具名）；
  2. `expected_structure` markers（fake 可验证部分以 `FAKE_CHECKABLE` 映射检查；
     `risk_warning / refusal_or_clarify / tool_trace` 为 spec-only，fake 下只记录不判失败）；
  3. `must_contain` 数字/标注子串必须出现在 `AgentLoop.run()` 输出中。
  `fake_expect=known_gap` 的 8 条用例预期在 fake 下缺 spec-only 结构，
  不计入 headline 通过率，计入失败分类。
- `results_fake.json / results_real.json` — 最近一次跑出的基线产物。

## 链路归属

fake LLM 模板与 `src/hero_quant/api/server.py::_FakeLLM` 同口径；
被测循环为 `AgentLoop.run`（`src/hero_quant/agent/loop.py:616`），
工具来自 `tools/registry`，不断链、不碰线上数据。

---

# Golden set v1（W2-01 · PR-113/114/115）

`golden/golden_v1.jsonl` 是 G1 验收口径的 50 条评测集（PR-113），配套 runner 产出
成功率 / 工具选择 / grounding / p50·p95 / 单任务成本（PR-114），每次调用记录
provider·model·tokens·成本（PR-115，`src/hero_quant/llm/cost.py`）。

## 一键重跑

```bash
python evals/run_golden.py --mode fake     # 无需 key：只验证评测管道与断言机器
python evals/run_golden.py --mode real     # 需真实后端；无 key 时如实 skip
python scripts/eval_real.py                # 等价于 --mode real（PR-112 入口）
```

产物：`evals/results_golden_fake.json` / `evals/results_golden_real.json`。

## 字段（每条 case）

`id / category / query / expected_tools / expected_contains / expected_not_contains /
max_latency_ms / expect_grounding / expect_refusal / expect_no_data / note`

- `expected_contains` 为**大小写不敏感子串**；`expected_not_contains` 命中即失败。
- `expect_grounding`：`true/false` 参与 grounding P/R 统计，`null` 表示该用例不判 grounding。
- `expect_refusal`：拒答判定是**关键词启发式**（`run_golden.REFUSAL_MARKERS`），
  报告里固定标注 `refusal_detection=keyword_heuristic`，不冒充语义判定。
- `expect_no_data`：空数据场景必须**显式说明没数据**（`NO_DATA_MARKERS`）；
  模型编造数值即 `no_data_not_declared`，归入 `honesty_gap`。

## 分类覆盖（50 条）

| category | 条数 | 覆盖点 |
|---|---|---|
| market_data | 8 | 单工具行情、名称→代码、指数/美股/加密 |
| fundamentals | 4 | 本 build 无基本面源：验路由与「不编造」 |
| indicator_factor | 8 | SMA/BOLL/RSI/MACD/sharpe/回撤/因子 |
| correlation | 3 | 两两相关、中文名映射 |
| backtest | 8 | 参数化策略、PIT 校验、引擎列表、成本 |
| multi_tool | 6 | 2~3 工具编排 |
| grounding_refusal | 6 | 预测点位/稳赚/内幕/编造/代下单/保收益 |
| authz_denied | 3 | 跨租户导出、提示注入、凭据列举 |
| zh_terms | 4 | 涨停/除权除息/北向资金/市盈率 |

## 指标口径（`evals/metrics.py`）

- 任务成功率：全部断言通过（含 within `max_latency_ms`）的用例占比。
- 工具选择：实际执行过的工具名集合（`ToolCallRecorder` 包 `TOOL_REGISTRY.func`，退出即还原）
  对比 `expected_tools`，给 micro precision/recall/F1 + 精确匹配率。
- grounding：`expect_grounding` 标签 vs `LoopResult.grounding_verified`，给 P/R/F1。
- 延迟：p50 / p95 / max（线性插值，不依赖 numpy）。
- 成本：价目只取 `llm/catalog.py::MODEL_CATALOG`；目录外模型 `cost_usd=null` 单列
  `unpriced_cases`，**不并入 total 冒充完整成本**。
  - `cost.by_model`：逐 provider/model 的 calls / input·output tokens / cost，各桶求和必须等于
    `total_usd`（可对账）。
  - `cost.estimated_cases`：后端没回 usage、由字符启发式估算的用例数；估算值不能用于对外计费对账。
  - 结果文件另记 `cost_accounting`（pricing_source / unit / unpriced_policy），保证事后能追溯口径。

## Fail-closed 约定

- fake 模式**不产出模型质量结论**（`quality_metrics=null` + `quality_note`）。
- real 模式启动先做一次 `health_check`；整条 provider 链不可用时写
  `status=skipped / reason=no_llm_backend`（含脱敏后的 per-provider error code），
  **不写任何通过率**，退出码 0。
- 回归门默认关闭；CI 要阻断时传 `--min-pass-rate 0.8`（先做 nightly 非阻断，
  阈值以首轮真实基线 + 允许回退幅度设定）。

## CI（`.github/workflows/eval.yml`）

- `golden-fake`：PR 触及 `evals/**`、`src/hero_quant/llm/**`、`src/hero_quant/metrics/**` 时必跑，
  **阻塞**。门禁三条：50 条齐全、无 `loop_crash_or_hang`、`quality_metrics` 必须为 `null`。
- `golden-real`：nightly + 手动触发，`continue-on-error: true`（PR-114：先非阻断）。
  有 key 才真跑；无 key 时 runner 自己写 `skipped/no_llm_backend`，作业仍是绿。
- 转 blocking 的时机：拿到首轮真实基线后，用 `--min-pass-rate` 固定允许回退幅度，再移除 `continue-on-error`。

## 成本指标归档

指标收集器统一注册在 `src/hero_quant/metrics/__init__.py`：
`hero_quant_llm_tokens_total{provider,model,kind}`、`hero_quant_llm_cost_total{provider,model}`、
`hero_quant_llm_latency_seconds{provider,model}`、`hero_quant_llm_failover_total{src,dst,reason}`。
`llm/cost.py` 只负责算账与调 `inc_llm_*`，不再自建 Counter（避免同名重复注册把整组指标降级为 None）。

## 待接（不在本批白名单）

- `api/server.py` 的 `LLMGateway` 接线（启动 health_check + 响应带 `degraded/reason`）
  按批次约定留到最后串行。

