# hero-quant 超越 vibe-trading 生产级能力设计

> **Date:** 2026-08-20  
> **Scope:** 全量 0-12月 超越 (P0/P1/P2)  
> **Team:** 2-3人 允许Rust (1内核+1体验)  
> **Success:** 商业化SaaS — 多租户+因子市场+计费 Ledger可审计+Live可熔断，vibe无法做到  
> **Approach:** B 微内核 Trait 插件化 (Rust向量化 + 事件驱动 + 向量路由)  
> **Status:** Design Approved — 5节全部认可

---

## 0. 背景与决策

- hero-quant 当前 ~45py 7k LOC 17工具 2真Loader vs vibe-trading 2253文件 278k LOC 79工具 39 loaders 472因子 — 功能广度 15-20% / 内核机制 60% (前序探查 exp-1~5, ora-1)
- 已生产级且反超：`governance/ledger.py:86 0600+fsync` `trace.py:421 tmp→link` `dedup.py:614 PG三态` `memory/store.py:155 FTS5` — 放大此底座降维打击
- 约束共识：2-3人/Rust允许/商业化SaaS为唯一超越标准/三不做(不追数量、不做Electron/K8s、不追全接口兼容、破重建Trait)
- 为什么B最优：渐进演进A债累积、全复刻C 278k臃肿必爆；B以30k LOC打278k，10倍效率即护城河

## 1. 架构总览 (Section 1/5 ✅)

```
                    ┌─ Tauri Frontend (5路由) ─┐
User NL → MCP向量路由(TopK5) → Kernel Loop ←→ Grounding三级校验
                ↓                     ↓           ↓
        Data Plugins(5 Loader Trait)  Quant(60 Rust算子)  Engine(事件驱动单引擎)
                ↓                     ↓           ↓
        Provenance+fallback → Polars/Arrow宽表 → Bar→Signal→Execution→positions/fills
                ↓                                         ↓
        Research(Hypothesis/StrategyStore/Decay) ←→ Exec(Shadow/Live风控中台+Temporal)
                ↓                                         ↓
        PG RLS多租户 + Otel/Circuit + Ledger/Trace审计 → Billing
```

- **Kernel** ~7k→30k LOC 微内核，Plugins热插拔
- **Sidecars:** Postgres RLS + Temporal + Otel Collector + Redpanda(仅P2实时) — 单机+sidecar交付，Day1 RLS预留，暂不上K8s
- **Rust边界:** 仅60算子+引擎热路径用Rust(PyO3)，其余Python；Polars/Numba过渡
- **三态一体:** 同一套引擎代码 Backtest→Paper→Live，Temporal重放

## 2. 组件与职责 (Section 2/5 ✅)

| 组件 | 职责 | 关键文件/接口 | Owner |
|---|---|---|---|
| **Kernel** | Loop状态机10控制点/Tracker/Governance | `src/hero_quant/agent/loop.py` `context.py:109` `grounding.py:49` `trace.py:421` `policies.py:22` | 内核 |
| **Data Plugins** | 5 Trait Loader: 行情/财务PIT/资金流/链上/自建+Registry+1%阻断 | `src/hero_quant/data/registry.py:197` `loaders/tencent.py:64 yahoo.py:9` → Trait | 体验 |
| **Engine** | 事件驱动单引擎：Bar对齐次日开盘+资金预检比例缩放+historical_base_price+limit_band+转成本 | `src/hero_quant/backtest/engine.py:324` `validation.py:29` `metrics.py:9` | 内核 |
| **Quant** | 60向量算子：sma/ema/rsi/bollinger/macd + options/fixedincome/credit/risk/var_backtest/multipletesting | `src/hero_quant/quantlib/indicators.py:260` → Rust | 内核 |
| **Agent** | MCP 20精选只读+12 Skill按需向量路由TopK5 | `src/hero_quant/tools/registry.py:47` `skills/loader.py:19` | 体验 |
| **Research** | Hypothesis 5状态+StrategyStore Artifact/Bench/Decay+Alpha bench | `src/hero_quant/memory/store.py` 新增 `strategy_store/` `hypotheses/` | 体验 |
| **Exec** | ShadowAccount 3-5规则+5类归因 + Live Pre/At/Post风控+kill-switch + Scheduled Cron | `src/hero_quant/checkpoint/temporal.py:44` `sandbox/` `security/` | 内核 |
| **Sidecars** | PG RLS隔离+Ledger计费桩+Otel三档+Heartbeat四层+Circuit双桶 | `governance/ledger/dedup.py` `telemetry/otel/heartbeat/circuit` | 内核 |
| **Frontend** | 5路由精品：Dashboard/Research/Backtest/Live/Risk + ECharts/KaTeX + Tauri | `frontend/src/App.tsx:36` `pages/Chat/Research/Monitor` `store/` | 体验 |

- **分工:** 1内核(Rust/Engine/Sidecar/Exec) +1体验(Data/Skill/Research/Frontend)，并行不冲突

## 3. 数据流 (Section 3/5 ✅)

**批链路 P0:** `用户NL → MCP向量路由选TopK5 → Loader Trait拉bars(Provenance{source,unit,interval}+tushare_fallbacks链) → Quant Rust向量算子(Arrow宽表NaN传播) → 事件引擎(信号→次日开盘clip→资金预检缩放) → positions/fills/metrics/tearsheet → Grounding三级校验(价区间/仓位/风控) → Hypothesis/Decay → Shadow(归因missed/noise/early/late/overtrade) → Ledger/Trace审计 → Otel`

**实时链路 P2:** `WS Tick → Redpanda流 → 流式因子(<200ms) → 增量回测 → Live风控中台` — 接口P0预留，P2真流

**关键不变量:** PIT `weights_on≤price_date` 抛错、混币种拒绝、首bar 1% cross_source阻断、sidecar `tmp→fsync→link EEXIST不覆`

## 4. 错误处理 (Section 4/5 ✅)

| 层 | 策略 | 文件 |
|---|---|---|
| **数据** | Loader指数退避3次+jitter→fallback链→1%阻断→synthetic兜底 | `data/loaders/*` `registry.py:111` |
| **Agent** | RetryPolicy 指数退避+BudgetBreaker滑动窗口熔断+Grounding失败→correction_prompt 3次内重跑否则safe_fallback | `agent/policies.py` `grounding.py` |
| **回测** | PIT/非正价/混币种ValidationError+资金成比例缩放保权重+allow_nonpositive_prices开关 | `backtest/validation.py:29` `engine.py:45` |
| **执行** | Pre-trade(限额/持仓/币种)→order_guard→kill-switch→Post归因，Shadow 5类归因 | `live/order_guard` `shadow_account/` |
| **系统** | CircuitBreaker 50%/60s→open 30s→half 5探针 + Saga补偿 `Command(goto=compensate)` + Temporal重放 + Trace RLock+_safe_sidecar_path | `telemetry/circuit.py:17` `checkpoint/temporal.py` |

## 5. 测试策略 (Section 5/5 ✅ TDD强制)

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

- **粒度:** 每任务2-5min：写测试→见红→最小实现→见绿→提交 (writing-plans标准)
- **金字塔:**
  - 单测: 60算子边界(NaN/inf/空)、Loader Trait(1%阻断/PIT/混币种)、Engine(PIT正逻辑、多标权重、turnover)、Tool并发安全
  - 契约: @tool(JSONSchema+is_concurrency_safe)+MCP 20精选定义排序稳定 get_definitions
  - 集成: Loader→Engine→Tearsheet→Grounding→Trace 全链快照 (tencent live→backtest→tearsheet→ledger verify)
  - E2E: Playwright 5路由+资金影子对账日跑+Shadow归因
  - 性能: 回测5x/成本3x基准门进CI (pytest-benchmark)
- **多租户:** PG RLS隔离从L1植入，`dedup` `derive_key(tenant/workflow/step)` + `ledger` `tenant`字段
- **命令:** `pytest -q` 全绿 `pytest tests/test_backtest_engine.py::test_pit -v` 点测

## 6. 成功标准与度量

- **硬指标:** 同策略回测 hero比vibe快5倍、推理成本低3倍、`MCP工具调用准确率>90%`
- **SaaS指标:** 多租户RLS隔离、因子市场“因子即资产”上架→计费→归因闭环、Ledger可审计+Live可熔断 (机构级)
- **质量门:** ` Ledger verify()` 全链通过、`Trace 50k侧车` 不丢、`Grounding三级校验` 0幻觉价

## 7. YAGNI / 三不做 (边界✅)

1. **不追数量只追质量:** 79工具→20精选、84技能→12精品、472因子→60核心+PGVector长尾，vibe 70%低频
2. **不做 Electron/K8s:** 桌面Tauri(体积1/10)、部署单机+PG/Temporal/Otel 3 sidecar，K8s延后
3. **不追全接口兼容:** 破兼容重建 `SourceTrait/EngineTrait/OperatorTrait/SkillTrait`，不逐文件兼容vibe 472py
4. **功能刹车:** 每新增1 Loader/Skill需证明>20%用户用或替换1个

## 8. 风险与取舍

| 风险 | 应对 |
|---|---|
| Rust学习慢 | 仅1人攻坚Rust算子，其余Polars/Numba过渡，非瓶颈不Rust化 |
| 追平焦虑抄功能 | 功能刹车+YAGNI，宁缺毋滥 |
| 过早SaaS拖慢 | L1仅RLS隔离+Ledger计费桩，L3再完整计费 |
| 实时过度设计 | P0轮询+增量模拟，P2真Redpanda流，接口先行 |
| 单机→分布式陷阱 | 复用Dedup/Trace单机优势+PG/Temporal即可分布式，无需K8s |

---

> 设计依据：`docs/plans/2026-08-20-slim-research-design.md` 8大模式 + 前序exp/ora探查  
> 下一步：`writing-plans` → `docs/plans/2026-08-20-hero-quant-surpass.md` 任务级TDD计划 → Subagent-Driven Build
