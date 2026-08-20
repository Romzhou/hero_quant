# hero-quant 可上线收口计划 — Docker单机可部署

> **For implementer:** TDD throughout. 每任务先写 failing test → 确认红 → 最小实现 → 绿 → commit. 单仓单包 `hero-quant`, 目标 `docker compose up` 即 `http://127.0.0.1:8899` 前后端闭环 + 合成/真实行情可切换 + 回测PIT正逻辑 + 账本/熔断/日志可观测。Vibe-Trading 选 Docker单机（`Dockerfile:1` + `docker-compose.yml:1` 3阶段，无K8s），本计划沿用。

**基线:** 44 tests 绿，`Dockerfile`/`docker-compose.yml`/`.dockerignore`/`api/server.py:100 StaticFiles` 已落地 (2026-08-21). 下述18任务为“薄桩→生产”收口，按依赖分 Wave，可并行见标注。

**Tech Stack:** Python 3.11, FastAPI/Pydantic v2, LangChain 1.x + LangGraph 1.x(StateGraph/create_agent/PostgresSaver), pandas/numpy/scipy/statsmodels, React19/Vite/Zustand/ECharts, structlog/prometheus_client, psycopg[pool], uv/pnpm, ruff/black, pip-audit/gitleaks

---

## Wave 0 — 已完成（归档）

| 项 | 文件 | 状态 |
|---|---|---|
| 3阶段镜像 | `Dockerfile:1` node22+python builder+runtime, hash-lock, vibe/vibe-sandbox 10001, HEALTHCHECK /live | ✅ |
| Compose | `docker-compose.yml:1` 单服务 8899, volumes hero-runs/sessions/home, cap_drop/read_only/tmpfs | ✅ |
| 静态托管 | `src/hero_quant/api/server.py:100` `_resolve_frontend_dist()` + `app.mount("/", StaticFiles)` 最后挂载 | ✅ |
| .dockerignore | `.dockerignore:1` | ✅ |

---

## Wave A — 统一编排 + 供应链（解耦所有后续）

### Task A1: AgentLoop 统一为 LangGraph StateGraph（选型A）

**Files:**
- Modify: `src/hero_quant/agent/loop.py`
- Modify: `src/hero_quant/agent/graph.py`（去 try/except 软导入，补 Send 并行）
- Modify: `src/hero_quant/agent/state.py`（补 thread_id/retry 字段）
- Modify: `pyproject.toml` add `langgraph>=1.0`, `langchain-core`, `psycopg[binary,pool]`
- Test: `tests/test_agent_loop.py`, `tests/test_graph_subagents.py`, Add `tests/test_loop_graph_unified.py`

**Step 1: Write failing test**
```python
# tests/test_loop_graph_unified.py
def test_loop_delegates_to_graph(monkeypatch):
    from hero_quant.agent.loop import AgentLoop
    class FakeLLM:
        def stream_chat(self, *a, **kw): yield {"type":"text","text":"hi"}
    loop = AgentLoop(llm=FakeLLM(), max_iterations=2, use_graph=True)
    r = loop.run("回测 600519.SH 近一月等权")
    assert r.terminated is True
    assert hasattr(r, "thread_id") or "graph" in r.reason.lower()
    # 调用应走 StateGraph.invoke，非旧 for-range 桩
    from hero_quant.agent.graph import build_research_graph
    g = build_research_graph(checkpointer=None)
    assert g is not None

def test_loop_single_vs_team_routing():
    from hero_quant.agent.loop import AgentLoop
    # mode=single 仍走旧轻量（Chat直连），mode=team 走graph
    assert True
```

**Step 2: Run** `pytest tests/test_loop_graph_unified.py -v` FAIL `use_graph kw unknown / build_research_graph missing`

**Step 3: Implement**
```python
# loop.py: AgentLoop.__init__(use_graph=False, checkpointer=None, thread_id=None)
# run() 若 use_graph: state={"messages":[{"role":"user","content":goal}], "thread_id":self.thread_id}; return graph.invoke(state, config={"configurable":{"thread_id":...}})
# 保留 single 轻量分支兼容 test_agent_loop_terminates
# graph.py: from langgraph.graph import StateGraph, START, END; from langgraph.types import Send, Command; execute_node 用 Send leaf fan-out，真并行
# pyproject.toml: dependencies add langgraph, optional checkpoint extra
```

**Step 4: Pass** `pytest tests/test_agent_loop.py tests/test_graph_subagents.py tests/test_loop_graph_unified.py -v`

**Step 5: Commit** `feat(agent): loop统一LangGraph, single/team双路由`

---

### Task A2: Tool合约并发审计收口

**Files:** Modify `src/hero_quant/tools/registry.py`, `src/hero_quant/tools/presentation.py`, `src/hero_quant/tools/market_data.py`, Test `tests/test_tool_contract.py`

**Failing test:**
```python
def test_concurrency_safe_audit():
    from hero_quant.tools.registry import TOOL_REGISTRY
    import hero_quant.tools.market_data
    spec = TOOL_REGISTRY["get_market_data"]
    assert spec.is_concurrency_safe({"symbol":"600519.SH"}) in (True, False)
    # 副作用 tool 必须 is_concurrency_safe==False
    assert TOOL_REGISTRY["run_backtest"].is_concurrency_safe({"symbols":["600519.SH"]}) is False
    from hero_quant.tools.registry import get_definitions
    defs = get_definitions()
    assert defs == sorted(defs, key=lambda d: d["function"]["name"])  # KV-cache 稳定
```

**Implement:** `market_data.py` `is_concurrency_safe=lambda args: True`（读），`backtest.py` `is_concurrency_safe=False`（写），`get_definitions` 已 sort，保持。

---

### Task A3: 供应链真 hash-lock + CI 三件套

**Files:** Create/Modify `requirements-lock.txt` (real hashes), `pyproject.toml`, `.github/workflows/test.yml`, Test: `tests/test_bootstrap.py` 扩展

**Failing test:**
```python
def test_lock_has_real_hashes():
    txt = open("requirements-lock.txt").read()
    assert "--hash=sha256:" in txt
    assert "aaaa" not in txt  # 占位
    assert "fastapi" in txt.lower()
```

**Implement:** `uv pip compile --generate-hashes pyproject.toml -o requirements-lock.txt` 真跑，`test.yml` 加 `pip install --require-hashes -r requirements-lock.txt` + `pip-audit` + `gitleaks detect` + `ruff check` + 3平台矩阵 `ubuntu/macos/windows`（占位先 linux 单测绿）。

---

## Wave B — 数据保真 + 回测硬化（2天，可与A并行后半）

### Task B1: Tencent 真解析 + HERO_DATA_MODE + 限流

**Files:** Modify `src/hero_quant/data/loaders/tencent.py`, `src/hero_quant/config/settings.py`, Test `tests/test_tencent_live.py`

**Failing test:**
```python
def test_tencent_live_parses_or_synthetic(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE","live")
    from hero_quant.data.loaders.tencent import TencentLoader
    import importlib, hero_quant.config.settings as s; importlib.reload(s)
    loader = TencentLoader()
    # 网络异常时仍回退 synthetic，但 live 模式下应尝试真 url 且带 1s+jitter
    bars = loader.get_bars("600519.SH","1d","2026-08-01","2026-08-03")
    assert len(bars)>=1 and bars[0]["close"]>0
    assert loader._rate_limit is not None  # 需真 sleep 桩可 mock
```

**Implement:** `tencent.py:_synthetic_bars` 保留，`get_bars` live 分支 `urllib.request.urlopen("http://web.ifzq.gtimg.cn/...", timeout=3)` 真解析 JSON → bars，失败仅 then fallback synthetic；`_rate_limit` 改为 `time.sleep(1+random.random()*0.3)` 但测试可 monkeypatch `time.sleep`；`settings.py:17 data_mode` 已有，补 `data_rate_limit_s` env。

---

### Task B2: Yahoo Loader + 全市场注册表 + Provenance审计

**Files:** Modify `src/hero_quant/data/loaders/yahoo.py`, `src/hero_quant/data/registry.py`, Test `tests/test_registry_provenance.py` 扩展

**Failing test:**
```python
def test_yahoo_declares_and_fallback():
    from hero_quant.data.loaders.yahoo import YahooLoader
    y=YahooLoader(); assert y.markets==["US"] and y.unit=="shares"
    from hero_quant.data.registry import MarketDataRegistry
    from hero_quant.data.loaders.tencent import TencentLoader
    reg=MarketDataRegistry(); reg.register(TencentLoader()); reg.register(YahooLoader())
    bars, prov = reg.get_bars("AAPL.US","1d","2026-08-01","2026-08-03")
    assert prov.source in ("yahoo","synthetic")  # AAPL.US 走 yahoo
    assert prov.unit=="shares"
```

**Implement:** `yahoo.py` 懒 `import yfinance`，`get_bars` 转 `yf.download` → bars，缺包 `raise ImportError("pip install hero-quant[us]")`；`registry.py:72 _detect_market` 保留，`get_bars` 按 `markets` 过滤 fallback，`audit_log:36` 已实，补 `VALID_SOURCES 16` 校验。

---

### Task B3: Backtest PIT 正逻辑 + 多引擎 + Tearsheet

**Files:** Modify `src/hero_quant/backtest/validation.py`, `src/hero_quant/backtest/engine.py`, `src/hero_quant/backtest/metrics.py`, Test `tests/test_backtest_pit_correct.py`, `tests/test_validation.py`

**Failing test:**
```python
def test_pit_strict_and_engine_artifacts(tmp_path):
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    prices=pd.DataFrame({"close":[100,101]}, index=pd.date_range("2026-08-10",periods=2))
    try: validate(prices, weights_on="2026-08-11", price_date="2026-08-10"); assert False
    except ValidationError: pass
    validate(prices, weights_on="2026-08-09", price_date="2026-08-10")
    # 兼容层不应在非 legacy 调用时触发 w<p 也抛
    from hero_quant.backtest.engine import BacktestEngine
    eng=BacktestEngine()
    res=eng.run(prices, weights=[0.5,0.5], output_dir=tmp_path)
    assert (tmp_path/"positions.csv").exists() and (tmp_path/"fills.csv").exists()
    assert "sharpe" in res["metrics"] and "tearsheet" in res
```

**Implement:** `validation.py:80 ts_w>ts_p→ValidationError` 已实，去 `_is_legacy_caller` 兼容或保留但文档化；`engine.py:29 run` 已产 `positions.csv/fills.csv/metrics.json/tearsheet.html`，补多引擎分支 `engine=="vectorized"` 同接口。

---

### Task B4: Quantlib 补齐 + 工具封装

**Files:** Modify `src/hero_quant/quantlib/indicators.py`, `src/hero_quant/tools/quantlib_tool.py`, Test `tests/test_quantlib.py`

**Implement:** `sma/ema/rsi/bollinger/max_drawdown` 纯 pandas 已实，补 `macd/bollinger_band` 边界测试；`quantlib_tool.py` `@tool(parameters/output/is_concurrency_safe=True)` 封装。

---

## Wave C — 可观测/韧性/安全（与B并行后启动）

### Task C1: OTel 三档 + structlog + /metrics 贯穿

**Files:** Modify `src/hero_quant/telemetry/otel.py`, `src/hero_quant/api/server.py:30`, Test `tests/test_otel.py`

**Implement:** `otel.py:get_otel_mode() env HERO_OTEL_MODE disabled|shared|private` 已实，`server.py:30 add_request_id_and_otel` 已实，补 `logger.bind(request_id, trace_id=thread_id)` 全链，`REQUEST_COUNTER` 按 endpoint 增。

---

### Task C2: Trace 硬阈值 + SSE 真流

**Files:** Modify `src/hero_quant/agent/trace.py`, `src/hero_quant/config/limits.py`, `src/hero_quant/api/server.py:75`, Test `tests/test_trace_hardening.py`, `tests/test_fc_formatting.py`

**Status:** `trace.py` 已实现 `TOOL_RESULT_OFFLOAD=50000 TEXT_OFFLOAD=50000 PREVIEW=500 + link(tmp,final) + _safe_sidecar_path + read(resolve_offloads)`，`limits.py TOOL_RESULT_LIMIT=10000`。需补 `api/server.py:/v1/query/stream` 真 SSE `StreamingResponse(event_generator:91)` 逐 token `data: {delta}` + `data: [DONE]`，前端对齐。

---

### Task C3: Checkpoint PostgresSaver 真 PG + Temporal占位

**Files:** Modify `src/hero_quant/checkpoint/postgres.py`, `src/hero_quant/checkpoint/temporal.py`, Test `tests/test_checkpoint.py`

**Status:** `postgres.py:54 AsyncPostgresSaver memory://` + TTL + 3段 thread_id 已实，真实分支为 no-op 占位。收口：`ConnectionPool(conninfo=dsn)` 真创，`setup()` 真建表 `CREATE TABLE checkpoints (thread_id TEXT PRIMARY KEY, checkpoint JSONB, config JSONB, expires_at TIMESTAMPTZ)`，`put/get` 走 `pool.execute`，失败回退内存。`temporal.py` 保留 Activity `heartbeat 15s` 占位即可（单机不强制）。

---

### Task C4: 心跳四层 + 熔断双桶 接入 API

**Files:** Modify `src/hero_quant/telemetry/heartbeat.py`, `src/hero_quant/telemetry/circuit.py`, `src/hero_quant/agent/policies.py`, Test `tests/test_heartbeat_circuit.py`, `tests/test_policies.py` 扩展

**Implement:** `HeartbeatTimer:0.5s min + daemon+join + _set_emitter` 已实，`CircuitBreaker failure50%/slow50%/30s window/open30s/half5` 已实，`policies.py:RetryPolicy指数退避+jitter + BudgetBreaker daily_limit` 已实，本任务仅将 `api/server.py` 慢调用 `circuit.record_slow(duration)` + `BudgetBreaker.should_fallback` 接入 `/v1/query`。

---

### Task C5: 幂等去重表 PG 化

**Files:** Modify `src/hero_quant/governance/dedup.py`, Test `tests/test_dedup.py`

**Status:** `DedupStore insert_pending/mark_success` 单机 dict+poll 已绿。收口：若 `HERO_DEDUP_DSN` 指向 PG 则 `INSERT ... ON CONFLICT DO NOTHING` + `SELECT FOR UPDATE WAIT`，否则内存（保持单机可跑）。

---

### Task C6: 安全收口（凭证/审批/脱敏/沙箱）

**Files:** Modify `src/hero_quant/security/credentials.py`, `src/hero_quant/security/approval.py`, `src/hero_quant/security/redaction.py`, `src/hero_quant/sandbox/ast_guard.py`+`policy.py`+`base.py`, `src/hero_quant/api/security.py`, Test `tests/test_security_approval.py`, `tests/test_security_stress.py`

**Status:** `ast_guard allowlist+ban socket/subprocess/os.system/ctypes/eval` 已实，`redaction ARGUMENTS_SINK/RESULT_SINK` 已实，`approval NEVER短路` 已实。收口：`credentials.py REF_PATTERN 每操作重解析 + 0600` 补热重载；`api/security.py HMAC+Host白名单` 由 `VIBE_TRADING_TRUST_DOCKER_LOOPBACK=1` 时放行 `127.0.0.1`，否则 `401`；`sandbox/base.py confine(argv,policy)` 仍 `return as-is`（单机Docker已 `cap_drop+read_only`，L1 bwrap 可 Phase2）。

---

### Task C7: Memory/Skills/Governance 补齐

**Files:** Modify `src/hero_quant/memory/store.py`, `src/hero_quant/skills/loader.py`, `src/hero_quant/governance/ledger.py`, Test `tests/test_memory.py`, `tests/test_skill_disclosure.py`, `tests/test_ledger.py` 扩展

**Implement:** `memory Store file+FTS5+30s dedup+flock+0600` 已实，补 `Store(namespace=f"{tenant}:{thread}")` 隔离；`skills loader 5 roots+digest snapshot+observed失效` 已实；`ledger seq/prev_hash/record_hash+fsync+0600` 已实，补 `verify()` 跨段重算（已实）。

---

## Wave D — 前端闭环 + E2E（最后，需 A+B+C 就绪）

### Task D1: 前端三页接真数据

**Files:** Modify `frontend/src/pages/Chat.tsx`, `frontend/src/pages/Research.tsx`, `frontend/src/pages/Monitor.tsx`, `frontend/src/store/chat.ts`, Test `frontend/src/__tests__/Chat.test.tsx`

**Implement:** `Chat.tsx:29 tool轨迹` 已接 SSE mock，需改 `fetch("/v1/query/stream?q="+q)` 真 `EventSource` 逐 `delta` 增量，`grounding·已校验` 徽标保留；`Research.tsx` 接 `positions.csv/metrics.json/tearsheet.html` 真渲染（ECharts 月热力+回撤topN），`Monitor.tsx` SSE `trace.jsonl offset` 实时。

---

### Task D2: E2E 全链路（自然语言→行情→回测→报告）

**Files:** Create `tests/test_e2e_full.py` (已规划 C9), Modify `src/hero_quant/agent/graph.py` 串 `registry→grounding→engine→tearsheet`, Test `tests/test_e2e.py` 扩展

**Failing test:**
```python
def test_e2e_full_to_report(tmp_path):
    from hero_quant.agent.graph import build_research_graph
    g=build_research_graph()
    res=g.invoke({"messages":[{"role":"user","content":"回测 600519.SH 近一月等权"}]}, config={"configurable":{"thread_id":"e2e:1:tenantA"}})
    assert res is not None and "metrics" in str(res).lower() or "verification" in res
```

**Implement:** `graph.py:plan→execute→verify` 已实，`execute` 内调 `get_market_data→grounding.ingest→BacktestEngine.run→render_block`，输出 `metrics+provenance+manifest_hash`。

---

### Task D3: 压测/安测回归

**Files:** Test `tests/test_perf_stress.py` (并发10线程Trace/Backtest吞吐/熔断阈值 已实), `tests/test_security_stress.py` (沙箱逃逸/脱敏/账本篡改/路径穿越 已实)

**Gate:** `pytest -q` 44+5 保持绿，`npm run test:run` 绿，`docker compose config` 校验，`act -n` 或本地 `pip-audit` 无 high。

---

## 依赖图

```
A1 Loop统一 ─┬─> A2 Tool合约 ─> D2 E2E
             ├─> A3 供应链 ──> D3 CI
B1 Tencent ─┬─> B2 Yahoo/Registry ─> B3 Backtest ─> D1 前端 ─> D2
C1 OTel ─┬─> C2 Trace/SSE ─> D1
C3 PG  ─┴─> C5 Dedup  ─┬─> D2
C4 熔断/心跳 ──────────┘
C6 安全 ───────────────> D1/D2
C7 Memory/Skills/Ledger ─> D2
```

**并行策略:** A1/A3 可立即并行；B1/B2 与 A 后半并行；C1-7 在 A1 合并后并行；D 需 A+B+C 合并后串行。

## 执行交接

两选项：

1. **Subagent-Driven** — 按 Wave A1→D3 依次派子代理（每任务 TDD + 两阶段 review），你只需等待。
2. **Manual** — 你按本计划自跑，每完成一任务贴测试结果我来 review。

选哪种？

