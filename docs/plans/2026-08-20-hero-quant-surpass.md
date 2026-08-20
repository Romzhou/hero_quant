# hero-quant 超越 vibe-trading 实现计划

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 0-12月分三阶段超越 vibe-trading生产级能力，以商业化SaaS为超越标准（多租户RLS+因子市场+Ledger计费+Live熔断），30k LOC打278k。

**Architecture:** B 微内核Trait插件化 — Kernel(Loop/Context/Grounding/Trace) + 5 Loader Trait/1事件驱动Engine/60向量算子Rust内核/MCP向量路由20工具/12 Skill按需/Sidecars(PG RLS+Temporal+Otel)+Tauri前端5路由。Day1定Trait边界，破兼容重建，三不做。

**Tech Stack:** Python 3.11 + Polars/Arrow + Rust PyO3(算子热路径) + FastAPI + Postgres RLS + Temporal + Redpanda(仅P2) + Zustand + ECharts + Tauri + Prometheus + structlog

---

## Milestone P0 0-3月 Foundation Parity — 能力追平 代码1/10

### Task 1: SourceTrait + Registry Trait化重构

**Files:**
- Modify: `src/hero_quant/data/registry.py`
- Create: `src/hero_quant/data/trait.py`
- Test: `tests/test_data_trait.py`

**Step 1: Write the failing test**
```python
# tests/test_data_trait.py
from hero_quant.data.trait import SourceTrait
def test_trait_registry_by_name():
    from hero_quant.data.registry import MarketDataRegistry
    r = MarketDataRegistry()
    # 新接口：get_bars通过trait分发
    assert hasattr(r, "register_trait")
    r.register_trait("test_src", SourceTrait)
    assert "test_src" in r.list_sources()
```

**Step 2: Run test — confirm it fails**
Command: `pytest tests/test_data_trait.py::test_trait_registry_by_name -v`
Expected: FAIL — `AttributeError: register_trait` / `ModuleNotFoundError: trait`

**Step 3: Write minimal implementation**
```python
# src/hero_quant/data/trait.py
from typing import Protocol
import pandas as pd
class SourceTrait(Protocol):
    name: str
    markets: list[str]
    unit: str  # board_lots|shares
    def get_bars(self, symbol: str, start: str, end: str, interval: str="1d") -> pd.DataFrame: ...
    def health(self) -> dict: ...
```
```python
# registry.py 新增
def register_trait(self, name: str, trait_cls): self._traits[name]=trait_cls
def list_sources(self): return list(self._traits.keys())
```

**Step 4: Run test — confirm it passes**
Command: `pytest tests/test_data_trait.py::test_trait_registry_by_name -v`
Expected: PASS

**Step 5: Commit**
`git add src/hero_quant/data/trait.py src/hero_quant/data/registry.py tests/test_data_trait.py && git commit -m "feat(data): SourceTrait + Trait registry"`

---

### Task 2: AKShareLoader 真实现 (P0 5 Loader之1)

**Files:**
- Create: `src/hero_quant/data/loaders/akshare_loader.py`
- Modify: `src/hero_quant/data/registry.py`
- Test: `tests/test_loader_akshare.py`

**Step 1: Write the failing test**
```python
def test_akshare_loader_fallback_synthetic():
    from hero_quant.data.loaders.akshare_loader import AKShareLoader
    loader = AKShareLoader()
    df = loader.get_bars("600519.SH", "2025-01-01", "2025-01-10")
    assert list(df.columns)[:3]==["open","high","low"]
    assert len(df)>=5
```

**Step 2:** `pytest tests/test_loader_akshare.py -v`  Expected FAIL not found

**Step 3:** 实现 `AKShareLoader`：try import akshare → 东财日线 → board_lots归一→ synthetic回退同tencent逻辑

**Step 4:** `pytest tests/test_loader_akshare.py -v` PASS

**Step 5:** `git add src/hero_quant/data/loaders/akshare_loader.py tests/test_loader_akshare.py && git commit -m "feat(data): akshare loader with synthetic fallback"`

---

### Task 3: CCXTLoader 真实现 (5 Loader之2)

**Files:**
- Create: `src/hero_quant/data/loaders/ccxt_loader.py`
- Test: `tests/test_loader_ccxt.py`

**Step 1:** `test_ccxt_loader_binance_spot()` 断言 `get_bars("BTC/USDT","2025-01-01","2025-01-05")` 列含 `volume` 且 `unit=="shares"`

**Step 3:** `ccxt.binance.fetch_ohlcv` → DataFrame 时间索引+Provenance

**Step 5:** `git commit -m "feat(data): ccxt loader binance/okx"`

---

### Task 4: cross_source 1% 从 warning 升级为阻断

**Files:**
- Modify: `src/hero_quant/data/registry.py`
- Test: `tests/test_data_cross_source_block.py`

**Step 1:**
```python
def test_cross_source_block():
    from hero_quant.data.registry import MarketDataRegistry
    r=MarketDataRegistry()
    # 注入两源首bar偏差>1%应抛CrossSourceError
    with pytest.raises(CrossSourceError):
        r._cross_source_check("600519.SH", df_a_close_100, df_b_close_103)
```

**Step 3:** `_cross_source_check` 中 `if abs(a-b)/a>0.01: raise CrossSourceError` 替代 `logger.warning`

**Step 5:** `git commit -m "fix(data): cross_source 1% block"`

---

### Task 5: 事件驱动单引擎 — Bar→Signal→Execution 三态

**Files:**
- Modify: `src/hero_quant/backtest/engine.py`
- Test: `tests/test_engine_event.py`

**Step 1:**
```python
def test_engine_event_pit():
    from hero_quant.backtest.engine import BacktestEngine
    # weights_on > price_date 应 ValidationError
    with pytest.raises(ValidationError):
        BacktestEngine().run(prices, weights_future, costs=0.001)
```

**Step 3:** 重构 `run` 为 `on_bar` 循环 + `historical_base_price` + `_align` 次日开盘 + `_execute_bars` 资金预检比例缩放 (复用现有 validation 已实现)

**Step 5:** `git commit -m "feat(backtest): event-driven single engine PIT"`

---

### Task 6: Quantlib 向量化基座 + 60算子首批6个 Rust stub

**Files:**
- Create: `src/hero_quant/quantlib/polars_base.py`
- Modify: `src/hero_quant/quantlib/indicators.py`
- Test: `tests/test_quantlib_vector.py`

**Step 1:**
```python
def test_sma_vector_equal_pandas():
    import pandas as pd
    s=pd.Series([1,2,3,4,5])
    from hero_quant.quantlib.indicators import sma
    from hero_quant.quantlib.polars_base import sma_polars
    assert sma(s,3).tolist()==sma_polars(s,3).tolist()
```

**Step 3:** `polars_base.py` 用 `pl.col.rolling_mean` 实现，indicators 保持pandas API不变，内部可切polars

**Step 5:** `git commit -m "feat(quantlib): polars base + vector parity"`

### Task 7: Quantlib 追加 options/fixedincome (覆盖 vibe 249函数首轮)

**Files:**
- Create: `src/hero_quant/quantlib/options.py` (bs_price/bs_greeks/iv)
- Test: `tests/test_quantlib_options.py`

**Step 1:** `test_bs_price_at_expiry_is_intrinsic()` 断言 `bs_price(S=100,K=100,T=0)=0`

**Step 3:** Black-Scholes 含退化路径

**Step 5:** `git commit -m "feat(quantlib): options bs pricing"`

---

### Task 8: MCP 20精选 + 向量路由 TopK5

**Files:**
- Create: `src/hero_quant/mcp/server.py`  `src/hero_quant/mcp/router.py`
- Test: `tests/test_mcp_router.py`

**Step 1:**
```python
def test_mcp_router_topk():
    from hero_quant.mcp.router import route
    tools=route("find momentum factors for 600519", k=5)
    assert len(tools)==5 and "compute_factor" in tools
```

**Step 3:** `router.py` 用 embeddings余弦或关键词权重选TopK，`server.py` FastMCP只读复用 `@tool`

**Step 5:** `git commit -m "feat(mcp): 20精选+vector router TopK5"`

---

### Task 9: Postgres+Temporal Sidecar docker-compose

**Files:**
- Modify: `docker-compose.yml` `src/hero_quant/checkpoint/postgres.py`
- Test: `tests/test_checkpoint_pg.py`

**Step 1:**
```python
def test_pg_saver_memory_fallback():
    from hero_quant.checkpoint.postgres import get_saver
    s=get_saver("memory://test")
    assert s is not None
```

**Step 3:** compose 新增 `postgres:16` + `temporal` + `otel-collector` sidecar，`get_saver` 真PG分支 `psycopg_pool`

**Step 5:** `git commit -m "feat(infra): PG+Temporal sidecar"`

---

### Task 10: BuildSystemPrompt + Grounding三级校验入prompt

**Files:**
- Create: `src/hero_quant/agent/prompt.py`
- Modify: `src/hero_quant/agent/context.py` `src/hero_quant/agent/grounding.py`
- Test: `tests/test_prompt_build.py`

**Step 1:**
```python
def test_build_system_prompt_injects_grounding():
    from hero_quant.agent.prompt import build_system_prompt
    p=build_system_prompt(skill_count=5, grounding_block="GND")
    assert "GND" in p and "HARD RULE" in p
```

**Step 3:** 260行简化版：Output Principles+Tool/Skill+Grounding+HARD RULE，`grounding.render_block()`注入system

**Step 5:** `git commit -m "feat(agent): build_system_prompt + 3-level grounding"`

---

## Milestone P1 3-6月 Experience Parity — 体验反杀

### Task 11: Agent Loop 并行只读工具池

**Files:** Modify `src/hero_quant/agent/loop.py`  Test `tests/test_loop_parallel.py`
Step1断言并行调用 `is_concurrency_safe=True` 工具耗时 < 串行/2
Step3 ThreadPoolExecutor并发，写工具串行

### Task 12: Context 向量折叠替代100字符

**Files:** Modify `src/hero_quant/agent/context.py`  Create `src/hero_quant/agent/embed.py`
Step1断言 >80%阈值触发向量摘要非首2尾2
Step3 embedding摘要+分级记忆

### Task 13: Frontend 5路由精品 Dashboard/Research/Backtest/Live/Risk

**Files:** Modify `frontend/src/App.tsx`  Create `pages/Dashboard.tsx pages/Live.tsx pages/Risk.tsx`
Step1 vitest断言5路由可达
Step3 lazy路由 + Tauri体积校验

### Task 14: ShadowAccount 2.0 熔断对接风控

**Files:** Create `src/hero_quant/shadow/*` Test `tests/test_shadow.py`
Step1 断言 `ShadowRule 3-5条 + 5类归因且 coverage>0`

### Task 15: ScheduledResearch Temporal Cron 5 playbooks

**Files:** Create `src/hero_quant/scheduled/*` Test `tests/test_scheduled.py`
Step1 断言 `cron 5-field + timezone ZoneInfo` 正确下次触发

### Task 16: Hypothesis/StrategyStore+Decay

**Files:** Create `src/hero_quant/hypotheses/registry.py` `strategy_store/store.py`
Step1 断言 `hyp_12hex` + `BenchResult ir/category`

---

## Milestone P2 6-12月 Moat — 换赛道

### Task 17: 实时流 Redpanda WS→流式因子<200ms

**Files:** Create `src/hero_quant/stream/*`  Modify `backtest/engine.py::on_tick`
Step1 benchmark断言增量因子延迟<200ms

### Task 18: 因子市场 多租户RLS+计费

**Files:** Modify `governance/ledger.py` (增加tenant/price)  Create `src/hero_quant/billing/*`
Step1 断言 `ledger.append(tenant=...)` + `verify()` + RLS隔离查询

### Task 19: E2E Playwright + 资金影子对账日跑

**Files:** Create `e2e/*.spec.ts`  `src/hero_quant/governance/reconcile.py`
Step1 `npx playwright test` 全绿 + 对账0差额

### Task 20: Rust核心抽离开源 + 性能门进CI

**Files:** Create `crates/quantlib/`  Modify `.github/workflows/ci.yml`
Step1 `cargo test` + `pytest-benchmark` 回测5x断言门

---

## 执行约束 (Superpowers Hard Gates)

- **TDD强制:** 每Task 先写测试见红再实现见绿再提交，删除测试前代码
- **Commit粒度:** 每绿一次提交 `git add <files> && git commit -m "feat: ..."`
- **YAGNI:** 每新增1 Loader/Skill需删/替1个或证明>20%用户用
- **分工:** 内核(Tasks 1,5-10,17,18,20) + 体验(Tasks 2-4,11-16,19) 并行不冲突

## 验证清单

- `pytest -q` 全绿 (36→~80 tests)
- `pytest tests/test_data_cross_source_block.py -v` 阻断生效
- `docker compose up --build` PG/Temporal/Otel 三sidecar健康
- `npx playwright test` 5路由E2E绿
- `pytest-benchmark` 回测5x/成本3x门通过
- `ledger.verify()` + `dedup wait_for` + `trace read` 审计全链

---

> 下一步执行：见下方Two execution options
