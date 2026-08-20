# 真英雄量化 (hero-quant) Implementation Plan

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 绿地新建 `D:\kaipanla-data\hero-quant` — 借鉴 Vibe-Trading 8大模式，实现极简投研 Agent 闭环（自然语言→行情→回测→报告），全市场可插拔 + 3页前端 + 可观测首日落地。

**Architecture:** 单仓单包 `hero-quant`，内核 `src/agent|data|backtest|tools|memory|governance|api`，`data/registry` 插件化（tencent/yahoo懒加载），FastAPI+SSE 前后端分离，`hero-quant[ashare,us,crypto,swarm,live]` extras。

**Tech Stack:** Python 3.11, FastAPI/Pydantic v2, LangChain 1.x(仅传输), pandas/numpy/scipy, DuckDB, React 19/Vite/Zustand/ECharts, structlog, prometheus_client, pytest/vitest, uv

---

## Task 1: 初始化空仓与工程基座

**Files:**
- Create: `D:\kaipanla-data\hero-quant\pyproject.toml`
- Create: `D:\kaipanla-data\hero-quant\README.md`
- Create: `D:\kaipanla-data\hero-quant\.gitignore`
- Create: `D:\kaipanla-data\hero-quant\src\__init__.py`
- Test: `D:\kaipanla-data\hero-quant\tests\test_bootstrap.py`

**Step 1: Write the failing test**
```python
# tests/test_bootstrap.py
def test_package_importable():
    import hero_quant
    assert hero_quant.__version__ == "0.1.0"
```

**Step 2: Run test — confirm it fails**
Command: `pytest tests/test_bootstrap.py -v`  Expected: FAIL — `ModuleNotFoundError: hero_quant`

**Step 3: Write minimal implementation**
```toml
# pyproject.toml
[project]
name = "hero-quant"
version = "0.1.0"
description = "真英雄量化 - 极简投研 Agent (borrowed from Vibe-Trading)"
requires-python = ">=3.11,<3.13"
dependencies = ["fastapi>=0.104","uvicorn[standard]","pydantic>=2","python-dotenv>=1","pandas>=2","numpy>=1.24","scipy>=1.10","httpx>=0.28","rich>=13","pyyaml>=6","langchain>=1.3","langchain-openai>=1","prometheus_client>=0.20","structlog>=24"]
[project.optional-dependencies]
ashare = ["tushare>=1.2","akshare>=1.12"]
us = ["yfinance>=0.2"]
crypto = ["ccxt>=4.5"]
dev = ["pytest>=7","pytest-cov","ruff>=0.9","black>=24"]
[tool.setuptools.packages.find]
where = ["src"]
```
```python
# src/hero_quant/__init__.py
__version__ = "0.1.0"
```

**Step 4: Run test — confirm it passes**
Command: `pytest tests/test_bootstrap.py -v`  Expected: PASS

**Step 5: Commit**
`git init && git add . && git commit -m "feat: bootstrap hero-quant 0.1.0"`

---

## Task 2: 配置单一入口（防 os.getenv 散落）

**Files:**
- Create: `src/hero_quant/config/settings.py`
- Test: `tests/test_config.py`

**Step 1: Write the failing test**
```python
# tests/test_config.py
def test_settings_loads_env(monkeypatch):
    monkeypatch.setenv("HERO_LLM_PROVIDER", "deepseek")
    from hero_quant.config.settings import Settings
    s = Settings()
    assert s.llm_provider == "deepseek"
    assert s.llm_model is not None

def test_no_raw_getenv_outside_config():
    import ast, pathlib
    allowed = pathlib.Path("src/hero_quant/config")
    for p in pathlib.Path("src").rglob("*.py"):
        if allowed in p.parents or p.parent == allowed: continue
        assert "os.getenv" not in p.read_text(encoding="utf-8"), f"raw getenv in {p}"
```

**Step 2: Run test — confirm it fails**
`pytest tests/test_config.py -v` FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**
```python
# src/hero_quant/config/settings.py
import os
from dataclasses import dataclass
@dataclass
class Settings:
    llm_provider: str = os.getenv("HERO_LLM_PROVIDER", "openai")
    llm_model: str = os.getenv("HERO_LLM_MODEL", "gpt-4o-mini")
    api_key: str | None = os.getenv("HERO_API_KEY")
    data_default_market: str = os.getenv("HERO_DATA_MARKET", "CN")
```
- 全仓仅此文件允许 `os.getenv`，CI gate 后续加 AST 扫描。

**Step 4: Run test — confirm it passes**
`pytest tests/test_config.py -v` PASS

**Step 5: Commit**
`git add src/hero_quant/config/settings.py tests/test_config.py && git commit -m "feat: config single entry (env gate)"`

---

## Task 3: Trace 原子落盘 + Sidecar（崩溃安全）

**Files:**
- Create: `src/hero_quant/agent/trace.py`
- Test: `tests/test_trace.py`

**Step 1: Write the failing test**
```python
def test_trace_atomic_write(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    w = TraceWriter(tmp_path / "trace.jsonl", sidecar_threshold=50)
    w.append({"type":"llm","content":"x"*100})
    assert (tmp_path / "trace.jsonl").exists()
    # sidecar 文件存在且 trace 指向它
    lines = (tmp_path / "trace.jsonl").read_text().strip().splitlines()
    import json; rec = json.loads(lines[0])
    assert "sidecar" in rec
    assert (tmp_path / rec["sidecar"]).exists()
```

**Step 2: Run test — confirm it fails** `pytest tests/test_trace.py -v` FAIL

**Step 3: Write minimal implementation**
- `TraceWriter.append(obj)`: `json.dumps` → 若 len>threshold 则 `tmp→fsync→os.replace→dir fsync` 落 sidecar，再写 `{"sidecar": relpath, "hash": ...}`
- 每次写后 `flush+os.fsync`，`_safe_sidecar_path` 防目录穿越。

**Step 4: Run test — confirm it passes** `pytest tests/test_trace.py -v` PASS

**Step 5: Commit** `git add src/hero_quant/agent/trace.py tests/test_trace.py && git commit -m "feat: crash-safe TraceWriter with sidecar"`

---

## Task 4: 上下文折叠与截断明示

**Files:**
- Create: `src/hero_quant/agent/context.py`
- Test: `tests/test_context.py`

**Step 1: Write the failing test**
```python
def test_context_compact_marks_truncation():
    from hero_quant.agent.context import ContextManager
    cm = ContextManager(max_chars=100)
    for i in range(20): cm.add("user", f"msg {i} " + "x"*20)
    compacted = cm.compact()
    assert compacted.truncated is True
    assert "TRUNCATED" in compacted.banner
    # 必须保留首尾，不静默丢
    assert "msg 0" in compacted.text
    assert "msg 19" in compacted.text
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- 按消息边界折叠，迭代摘要（mock LLM 摘要为 `[SUMMARY]` 占位），保留首2+尾2条原文，中间折叠。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: context compactor with truncation banner"`

---

## Task 5: Grounding 证据账本

**Files:**
- Create: `src/hero_quant/agent/grounding.py`
- Test: `tests/test_grounding.py`

**Step 1: Write the failing test**
```python
def test_grounding_blocks_hallucinated_price():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": 1500.0, "date":"2026-08-19"}])
    # 未在 ledger 中的价格必须被拦
    try:
        ledger.assert_price("600519.SH", 9999.0)
        assert False, "should raise"
    except GroundingError as e:
        assert "not in evidence" in str(e).lower()
    # 在 evidence 范围内的通过
    ledger.assert_price("600519.SH", 1500.0)
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `ingest(symbol, bars)` 记录 OHLC 证据，`assert_price` 校验价格在 `[low,high]` 或精确命中 close，失败抛 `GroundingError`。
- 提供 `render_block()` 输出 Ground Truth 块供 system prompt 注入。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: grounding ledger (price evidence gate)"`

---

## Task 6: Tool Registry 自动发现

**Files:**
- Create: `src/hero_quant/tools/registry.py`
- Create: `src/hero_quant/tools/__init__.py`
- Test: `tests/test_tool_registry.py`

**Step 1: Write the failing test**
```python
def test_tool_registry_discovers():
    from hero_quant.tools.registry import tool, TOOL_REGISTRY
    @tool(name="demo_add", description="add")
    def demo_add(a: int, b: int) -> int: return a+b
    assert "demo_add" in TOOL_REGISTRY
    assert TOOL_REGISTRY["demo_add"].description == "add"
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `@tool` 装饰器注册到全局 dict，校验 name 唯一、description 非空。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: tool registry auto-discovery"`

---

## Task 7: Data Registry + Tencent Loader (A股)

**Files:**
- Create: `src/hero_quant/data/registry.py`
- Create: `src/hero_quant/data/loaders/tencent.py`
- Test: `tests/test_data_registry.py`

**Step 1: Write the failing test**
```python
def test_registry_fallback_and_provenance(monkeypatch):
    from hero_quant.data.registry import MarketDataRegistry
    from hero_quant.data.loaders.tencent import TencentLoader
    reg = MarketDataRegistry()
    reg.register(TencentLoader())
    bars, prov = reg.get_bars("600519.SH", "1d", "2026-08-01", "2026-08-19")
    assert len(bars) > 0
    assert prov.source == "tencent"
    assert prov.unit in ("board_lots","shares")

def test_missing_extra_raises_actionable():
    from hero_quant.data.registry import MarketDataRegistry
    reg = MarketDataRegistry()
    try: reg.get_bars("AAPL.US","1d","2026-08-01","2026-08-19")
    except ImportError as e: assert "pip install" in str(e)
    else: assert False
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `MarketDataRegistry.register(spec)` + `get_bars` 逐loader fallback，记录 provenance。
- `TencentLoader` 用 stdlib http，无 extra；声明 `markets=["CN"], unit="board_lots"`。
- 无可用 loader 时 `ImportError("pip install hero-quant[us]")`。

**Step 4: Run test — confirm it passes** (mock http)

**Step 5: Commit** `git commit -m "feat: data registry + tencent loader (CN)"`

---

## Task 8: Yahoo Loader + 全市场可插拔

**Files:**
- Create: `src/hero_quant/data/loaders/yahoo.py`
- Modify: `src/hero_quant/data/registry.py`
- Test: `tests/test_yahoo_loader.py`

**Step 1: Write the failing test**
```python
def test_yahoo_loader_declares_unit():
    from hero_quant.data.loaders.yahoo import YahooLoader
    y = YahooLoader()
    assert y.markets == ["US"]
    assert y.unit == "shares"
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `YahooLoader` 懒导入 `yfinance`，缺失时 `ImportError("pip install hero-quant[us]")`。
- `get_bars` 复用同一接口，失败时 registry 继续 fallback。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: yahoo loader (US) pluggable"`

---

## Task 9: Backtest Engine 抽象 + Metrics

**Files:**
- Create: `src/hero_quant/backtest/engine.py`
- Create: `src/hero_quant/backtest/metrics.py`
- Test: `tests/test_backtest_engine.py`

**Step 1: Write the failing test**
```python
def test_backtest_engine_runs():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close":[100,101,102,101,103]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine()
    res = engine.run(prices, weights=[0.5,0.5])
    assert "equity" in res
    assert res["metrics"]["sharpe"] is not None
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `BacktestEngine.run(prices, weights, costs=0.0005)` 输出 equity, positions.csv, metrics（年化收益/夏普/最大回撤/换手）。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: backtest engine + metrics"`

---

## Task 10: Backtest Validation（PIT/单位/混币）

**Files:**
- Create: `src/hero_quant/backtest/validation.py`
- Test: `tests/test_validation.py`

**Step 1: Write the failing test**
```python
def test_validation_rejects_future_data():
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    prices = pd.DataFrame({"close":[100,101]}, index=pd.date_range("2026-08-10", periods=2))
    # 用未来收盘做当日权重应被拦
    try: validate(prices, weights_on="2026-08-09", price_date="2026-08-10")
    except ValidationError: pass
    else: assert False
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- 校验：权重日期≤价格日期（PIT）、拒绝混币种聚合、拒绝非正价格。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: backtest validation (PIT, unit, currency)"`

---

## Task 11: 轻量 Quantlib

**Files:**
- Create: `src/hero_quant/quantlib/indicators.py`
- Test: `tests/test_quantlib.py`

**Step 1: Write the failing test**
```python
def test_sma_rsi():
    from hero_quant.quantlib.indicators import sma, rsi
    import pandas as pd
    s = pd.Series([1,2,3,4,5])
    assert sma(s, 3).iloc[-1] == 4.0
    assert 0 <= rsi(s, 14).iloc[-1] <= 100
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `sma/ema/rsi/bollinger/max_drawdown` 纯 pandas 实现。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: quantlib indicators (sma/ema/rsi)"`

---

## Task 12: Memory 文件存储 + FTS5

**Files:**
- Create: `src/hero_quant/memory/store.py`
- Test: `tests/test_memory.py`

**Step 1: Write the failing test**
```python
def test_memory_write_and_search(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    ms.write("note1", "贵州茅台 600519 财报超预期")
    ms.write("note1", "贵州茅台 600519 财报超预期") # 30s内去重
    assert len(ms.search("茅台")) == 1
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- 文件 `~/.hero-quant/memory/*.md` + sqlite FTS5 索引（无分词器时 bigram 回退），30秒去重窗口，flock+原子写。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: memory store (file+FTS5+dedup)"`

---

## Task 13: Agent Loop 状态机

**Files:**
- Create: `src/hero_quant/agent/loop.py`
- Test: `tests/test_agent_loop.py`

**Step 1: Write the failing test**
```python
def test_agent_loop_terminates(monkeypatch):
    from hero_quant.agent.loop import AgentLoop
    # mock llm 返回一次工具调用后结束
    class FakeLLM:
        def stream_chat(self, *a, **kw): yield {"type":"text","text":"done"}
    loop = AgentLoop(llm=FakeLLM(), max_iterations=3)
    result = loop.run("测试")
    assert result.terminated is True
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `AgentLoop.run(goal)`: while循环，集成 context/grounding/trace，支持 `max_iterations/token_limit/user_stop` 终止。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: agent loop state machine"`

---

## Task 14: API Server (FastAPI + SSE + /metrics)

**Files:**
- Create: `src/hero_quant/api/server.py`
- Create: `src/hero_quant/api/security.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing test**
```python
def test_health_and_metrics():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app
    c = TestClient(app)
    assert c.get("/live").status_code == 200
    assert c.get("/metrics").status_code == 200
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `/live`存活, `/ready`就绪(不耗token), `/metrics`暴露 prometheus, `/v1/query` SSE 流，`security.py` HMAC+Host白名单。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: api server (live/ready/metrics/SSE)"`

---

## Task 15: Governance 轻量Hash链

**Files:**
- Create: `src/hero_quant/governance/ledger.py`
- Test: `tests/test_ledger.py`

**Step 1: Write the failing test**
```python
def test_ledger_chain(tmp_path):
    from hero_quant.governance.ledger import Ledger
    l = Ledger(tmp_path / "ledger.jsonl")
    l.append({"action":"order","symbol":"600519.SH"})
    l.append({"action":"order","symbol":"AAPL.US"})
    assert l.verify() is True
    # 篡改检测
    p = tmp_path / "ledger.jsonl"
    p.write_text(p.read_text().replace("600519","999999"))
    assert l.verify() is False
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- 每行 `seq/prev_hash/record_hash`，0600权限，fsync，`verify()` 重算链。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: governance ledger hash chain"`

---

## Task 16: Frontend 3页

**Files:**
- Create: `frontend/package.json`, `frontend/src/pages/Chat.tsx`, `Research.tsx`, `Settings.tsx`
- Test: `frontend/src/__tests__/Chat.test.tsx`

**Step 1: Write the failing test**
```tsx
// Chat.test.tsx
import {render, screen} from "@testing-library/react"
import Chat from "../pages/Chat"
test("chat renders", () => { render(<Chat/>); expect(screen.getByText(/对话/)).toBeInTheDocument() })
```

**Step 2: Run test — confirm it fails** `npm test` FAIL

**Step 3: Write minimal implementation**
- Vite+React19+Zustand+Tailwind，3路由懒加载，Chat对接 `/v1/query` SSE。

**Step 4: Run test — confirm it passes** `npm run test:run` PASS

**Step 5: Commit** `git commit -m "feat: frontend 3 pages (chat/research/settings)"`

---

## Task 17: E2E 回归（自然语言→回测→报告）

**Files:**
- Create: `tests/test_e2e.py`

**Step 1: Write the failing test**
```python
def test_e2e_query_to_report():
    from hero_quant.agent.loop import AgentLoop
    # 端到端：mock llm + 真 registry + 真 backtest，验证最终报告含 metrics 且价格经 grounding
    # 预期：report.metrics.sharpe 非空，report.grounding_verified == True
    assert False, "not implemented"
```

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- 串联所有模块，提供 `run_e2e("回测 600519.SH 近一月等权")` helper。

**Step 4: Run test — confirm it passes**

**Step 5: Commit** `git commit -m "feat: e2e query→backtest→report"`

---

## Task 18: CI 与供应链

**Files:**
- Create: `.github/workflows/test.yml`, `requirements-lock.txt` (uv compile)
- Test: 手动 `pip install --require-hashes -r requirements-lock.txt` 验证

**Step 1: Write the failing test**
- CI 未配置时 `test.yml` 不存在即 fail

**Step 2: Run test — confirm it fails**

**Step 3: Write minimal implementation**
- `uv pip compile --generate-hashes` 生成锁，workflow 含 hash校验 + pytest + ruff + pip-audit + gitleaks

**Step 4: Run test — confirm it passes** `act` 或 `pytest` PASS

**Step 5: Commit** `git commit -m "ci: hash-lock + audit + ruff"`

---

## 执行交接

Plan saved to `docs/plans/2026-08-20-hero-quant-plan.md`. Two execution options:

1. **Subagent-Driven** — 我按 task 1→18 依次 `sessions_spawn` 派子代理（每任务 TDD + 两阶段 review），你只需等待。
2. **Manual** — 你按本计划自跑，每完成一 task 贴测试结果我来 review。

Which approach?
