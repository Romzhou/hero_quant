# 真英雄量化 hero-quant v2 生产级重做 Implementation Plan

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 从 demo（18测）升级为接近可落地的生产级量化研究 Agent：用户自然语言提问 → LangGraph 编排（research-team Subagents）自动完成全市场行情→回测→报告闭环，具备金融工程复杂性与鲁棒性、全程可观测、鉴权/沙箱/成本熔断、多租户分层架构、CI/CD 供应链加固。

**Architecture:** 单仓单包 `hero-quant` 分层：`前端3页+live监控 / FastAPI+SSE / LangGraph StateGraph(plan→execute→verify)+create_agent叶子+Subagents研究团队+确定性风控节点 / tools(68)+skills(89)+memory(FTS5)/governance(ledger+manifest) / data registry 25源(CN保真) / backtest 3引擎+quantlib / infra(trace JSONL+sidecar+OTel Collector→Langfuse/Honeycomb, PostgresSaver checkpoint, Temporal Activity)`。沙箱分级路由 `L0 AST → L1 nsjail/bwrap → L2 本地Docker抽象→E2B`，凭证不入箱。

**Tech Stack:** Python 3.11, FastAPI/Pydantic v2, LangChain 1.x + LangGraph 1.x(create_agent, StateGraph, PostgresSaver), Temporal(可选), pandas/numpy/scipy/statsmodels, DuckDB, React 19/Vite/Zustand/ECharts, structlog, prometheus_client, OTel Collector, Cerbos/PDP sidecar, Presidio, pytest/vitest, uv/pnpm, Docker, psycopg, ruff/black, pip-audit/gitleaks

---

## Wave A — 可观测/安全骨架（先骨架，2周）

### Task A1: 分层注册 Seam + Scope分层

**Files:**
- Create: `src/hero_quant/core/scope.py`
- Create: `src/hero_quant/core/__init__.py`
- Test: `tests/test_scope.py`

**Step 1: Write the failing test**
```python
# tests/test_scope.py
def test_scope_chain_layers():
    from hero_quant.core.scope import Scope, ScopedLayers, create_scope, link_scope_parent
    parent = create_scope("research")
    child = create_scope("factor", parent=parent)
    layers = ScopedLayers(); layers.set(parent, {"tool": "tencent"}); layers.set(child, {"tool": "yahoo"})
    assert layers.merge(child)["tool"] == "yahoo"
    assert layers.merge(parent)["tool"] == "tencent"
```

**Step 2: Run test — confirm it fails**
Command: `pytest tests/test_scope.py -v` Expected: FAIL `ModuleNotFoundError: hero_quant.core.scope`

**Step 3: Write minimal implementation**
```python
# src/hero_quant/core/scope.py
from collections import defaultdict
class Scope: ...
def create_scope(key, parent=None): ...
class ScopedLayers:
    def set(self, scope, vals): ...
    def merge(self, scope): ...
    def chain_layers(self, scope): ...
def link_scope_parent(child, parent): ...
```

**Step 4: Run test — confirm it passes** `pytest tests/test_scope.py -v` PASS

**Step 5: Commit** `git add src/hero_quant/core/scope.py tests/test_scope.py && git commit -m "feat(core): scope分层注册"`

---

### Task A2: Trace JSONL 加固（阈值+侧车+HARD表演）

**Files:**
- Modify: `src/hero_quant/agent/trace.py`
- Test: `tests/test_trace_hardening.py`

**Step 1: Write the failing test**
```python
def test_trace_hard_threshold_and_hardlink(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    import json
    w = TraceWriter(tmp_path, sidecar_threshold=50000, hard_threshold=500)
    w.append({"type":"tool_result","tool":"get_bars","content":"x"*60000})
    lines = (tmp_path/"trace.jsonl").read_text().strip().splitlines()
    rec = json.loads(lines[0])
    assert "result_path" in rec and "preview" in rec
    assert (tmp_path/rec["result_path"]).exists()
    # 硬链发布不覆盖
    w2 = TraceWriter(tmp_path, sidecar_threshold=50000)
    w2.append({"type":"tool_result","tool":"get_bars","content":"x"*100})
    assert len((tmp_path/"trace.jsonl").read_text().strip().splitlines())==2
```

**Step 2: Run test — confirm it fails** `pytest tests/test_trace_hardening.py -v` FAIL — `sidecar_threshold` param mismatch or threshold 50
**Step 3: Write minimal implementation**
- 重构 `TraceWriter(dir_path)` 签名，双阈值 `TOOL_RESULT_OFFLOAD=50000 TEXT_OFFLOAD=50000 PREVIEW=500` 支持 `HERO_TRACE_*` env，侧车 `tmp(pid).txt→fsync→link(tmp,final) EEXIST失败不覆盖→fsync(dir)`，`read(resolve_offloads)` 加 `_safe_sidecar_path` allowlist。
**Step 4: Run test — confirm it passes** `pytest tests/test_trace_hardening.py -v` PASS
**Step 5: Commit** `git commit -m "feat(trace): 硬阈值+HardLink发布+read"`

---

### Task A3: OTel 三档遥测 + 结构化日志骨架

**Files:**
- Create: `src/hero_quant/telemetry/otel.py`
- Create: `src/hero_quant/telemetry/__init__.py`
- Modify: `src/hero_quant/api/server.py`
- Test: `tests/test_otel.py`

**Step 1: Write the failing test**
```python
def test_otel_sharing_modes(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE","disabled")
    from hero_quant.telemetry.otel import get_otel_mode
    assert get_otel_mode()=="disabled"
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator
    c = SessionTelemetryCoordinator(mode="disabled")
    assert c.sharing()=="disabled"
```

**Step 2: Run test — confirm it fails** `pytest tests/test_otel.py -v` FAIL ModuleNotFound
**Step 3: Write minimal implementation**
```python
# src/hero_quant/telemetry/otel.py
import os
def get_otel_mode(): return os.getenv("HERO_OTEL_MODE","disabled")
class SessionTelemetryCoordinator:
    def __init__(self, mode): self.mode=mode
    def sharing(self): ...
# api/server.py 加 structlog JSON + X-Request-ID 中间件 + otel export 占位
```

**Step 4: Run test — confirm it passes** `pytest tests/test_otel.py -v` PASS
**Step 5: Commit** `git commit -m "feat(telemetry): OTel三档+structlog骨架"`

---

### Task A4: 凭证 refs + approval ask/never + redact瀑布

**Files:**
- Create: `src/hero_quant/security/credentials.py`
- Create: `src/hero_quant/security/approval.py`
- Create: `src/hero_quant/security/redaction.py`
- Test: `tests/test_security_approval.py`

**Step 1: Write the failing test**
```python
def test_approval_never_shortcuts(tmp_path):
    from hero_quant.security.approval import ApprovalService, ApprovalPolicy
    svc = ApprovalService(mode="never")
    outcome = svc.request_sync(tool="run_backtest", reason="高风险")
    assert outcome=="rejected"
    from hero_quant.security.redaction import redact_payload
    assert redact_payload({"api_key":"sk-xxx"}, sink="arguments")["api_key"]=="***"
```

**Step 2: Run test — confirm it fails** `pytest tests/test_security_approval.py -v` FAIL
**Step 3: Write minimal implementation**
- `credentials.py`: `REF_PATTERN` + `resolve(ref)` 每操作重解析 + `shadow fail-loud` + `0600` 热重载占位
- `approval.py`: `ASK|NEVER effectiveApprovalPolicy(events)` 倒序折叠 + `never` 服务层短路 + `approval/asked+decided` 转内审计
- `redaction.py`: `ARGUMENTS_SINK最严 / RESULT_SINK放行content` + `Bearer/sk-/AKIA/JWT` 正则

**Step 4: Run test — confirm it passes** `pytest tests/test_security_approval.py -v` PASS
**Step 5: Commit** `git commit -m "feat(security): credentials+approval+redaction"`

---

### Task A5: 沙箱基座 L0 AST + L1 bwrap/路径allowlist + 抽象接口

**Files:**
- Create: `src/hero_quant/sandbox/base.py`
- Create: `src/hero_quant/sandbox/policy.py`
- Create: `src/hero_quant/sandbox/ast_guard.py`
- Test: `tests/test_sandbox_base.py`

**Step 1: Write the failing test**
```python
def test_sandbox_ast_and_allowlist(tmp_path):
    from hero_quant.sandbox.ast_guard import check_import_allowlist
    assert check_import_allowlist("import pandas\nimport socket") is False
    assert check_import_allowlist("import pandas\nimport numpy") is True
    from hero_quant.sandbox.policy import resolve_policy
    p = resolve_policy(mode="workspace-write", workspace_root=str(tmp_path))
    assert "workspaceRoot" in p
```

**Step 2: Run test — confirm it fails** `pytest tests/test_sandbox_base.py -v` FAIL
**Step 3: Write minimal implementation**
- `ast_guard.py`: allowlist `pandas/numpy/scipy/math/typing` + ban `socket/subprocess/os.system/ctypes/eval/__import__/requests`，深扫嵌套函数
- `policy.py`: `mode read-only|workspace-write|danger-full-access` + `canonicalPath` 真源 + `writableRoots = {workspaceRoot,/tmp}`
- `base.py`: `class BaseSandbox: execute(cmd)->(stdout,stderr,exit_code)` 抽象，`LocalShellBackend` 先直通，`confine(argv,policy)→wrappedArgv` 占位，`enforcement full/partial`

**Step 4: Run test — confirm it passes** `pytest tests/test_sandbox_base.py -v` PASS
**Step 5: Commit** `git commit -m "feat(sandbox): L0 AST+L1 policy抽象"`

---

### Task A6: 语义化工具合约（parameters+output+并发安全）

**Files:**
- Modify: `src/hero_quant/tools/registry.py`
- Create: `src/hero_quant/tools/presentation.py`
- Test: `tests/test_tool_contract.py`

**Step 1: Write the failing test**
```python
def test_tool_contract_schema_and_concurrency():
    from hero_quant.tools.registry import tool, TOOL_REGISTRY, get_definitions
    @tool(name="demo_safe", description="safe", parameters={"type":"object","properties":{"x":{"type":"string"}}, "required":["x"], "additionalProperties": False}, output={"type":"object","properties":{"ok":{"type":"boolean"}}}, is_concurrency_safe=lambda args: True)
    def f(x: str): return {"ok": True}
    assert TOOL_REGISTRY["demo_safe"].is_concurrency_safe({"x":"a"}) is True
    defs = get_definitions()
    assert any(d["function"]["name"]=="demo_safe" for d in defs)
```

**Step 2: Run test — confirm it fails** `pytest tests/test_tool_contract.py -v` FAIL `tool() got unexpected keyword parameters`
**Step 3: Write minimal implementation**
- 扩展 `@tool(name, description, parameters, output, is_concurrency_safe, timeoutMs)`，存 `ToolSpec` + `output {schema,render}` 必校验 `assertSupportedJsonSchema`，`get_definitions()` 拆 `toolOrder sort`+ KV-cache稳定，`presentAs native|code|both` 存根
**Step 4: Run test — confirm it passes** `pytest tests/test_tool_contract.py -v` PASS
**Step 5: Commit** `git commit -m "feat(tools): 语义化合约+并发安全"`

---

### Task A7: Function Calling 格式化落库（截断+分页+脱敏）

**Files:**
- Create: `src/hero_quant/tools/redaction.py` # 已在A4则扩展
- Modify: `src/hero_quant/agent/trace.py`
- Test: `tests/test_fc_formatting.py`

**Step 1: Write the failing test**
```python
def test_fc_truncate_and_redact():
    from hero_quant.tools.redaction import redact_tool_result
    big = "a"*15000
    res = redact_tool_result(big)
    assert "TRUNCATED" in res or len(res) <= 10000
    from hero_quant.agent.trace import TraceWriter
    # 50k侧车阈值已在A2测，此测10k模型截断
```

**Step 2: Run test — confirm it fails** `pytest tests/test_fc_formatting.py -v` FAIL
**Step 3: Write minimal implementation**
- `limits.py TOOL_RESULT_LIMIT=10000` + `truncate_tool_result(result,limit)`带`shown/total`声明，`fit_records`分页，`redact_tool_result` sink-aware

**Step 4: Run test — confirm it passes** `pytest tests/test_fc_formatting.py -v` PASS
**Step 5: Commit** `git commit -m "feat(trace): fc格式化+截断+脱敏"`

---

## Wave B — 研究团队 + CN保真（2周）

### Task B1: CN行情真解析与 HERO_DATA_MODE 开关

**Files:**
- Modify: `src/hero_quant/data/loaders/tencent.py`
- Modify: `src/hero_quant/config/settings.py`
- Test: `tests/test_tencent_live.py`

**Step 1: Write the failing test**
```python
def test_tencent_live_or_synthetic_flag(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE","synthetic")
    from hero_quant.data.loaders.tencent import TencentLoader
    import importlib, hero_quant.config.settings as s; importlib.reload(s)
    loader=TencentLoader()
    bars=loader.get_bars("600519.SH","1d","2026-08-01","2026-08-03")
    assert len(bars)>0
    assert bars[0]["close"]>0
```

**Step 2: Run test — confirm it fails** `pytest tests/test_tencent_live.py -v` FAIL env解析未接入
**Step 3: Write minimal implementation**
- `settings.py` 加 `data_mode=os.getenv("HERO_DATA_MODE","synthetic")`
- `tencent.py` 真解析 `qt.gtimg.cn` + `ValueError→synthetic` 仅在 `mode==synthetic` 或网络异常时回退，`em_get 1s+jitter` 限流占位

**Step 4: Run test — confirm it passes** `pytest tests/test_tencent_live.py -v` PASS
**Step 5: Commit** `git commit -m "feat(data): CN真解析+HERO_DATA_MODE"`

---

### Task B2: 多市场注册表扩展 + provenance审计页

**Files:**
- Modify: `src/hero_quant/data/registry.py`
- Test: `tests/test_registry_provenance.py`

**Step 1: Write the failing test**
```python
def test_registry_provenance_audit():
    from hero_quant.data.registry import MarketDataRegistry
    from hero_quant.data.loaders.tencent import TencentLoader
    reg=MarketDataRegistry(); reg.register(TencentLoader())
    bars,prov=reg.get_bars("600519.SH","1d","2026-08-01","2026-08-03")
    assert prov.source in ("tencent","synthetic")
    audit=reg.audit_log[-1]
    assert "symbol" in audit
```

**Step 2: Run test — confirm it fails** `pytest tests/test_registry_provenance.py -v` FAIL audit_log缺失
**Step 3: Write minimal implementation**
- `registry.py` 加 `VALID_SOURCES` 16源表 + `audit_log` 追 `symbol/source/unit/start/end` + Cross-source 1%回归占位

**Step 4: Run test — confirm it passes** `pytest tests/test_registry_provenance.py -v` PASS
**Step 5: Commit** `git commit -m "feat(data): registry provenance审计"`

---

### Task B3: Skills 渐进披露（digest两段式+热失效）

**Files:**
- Create: `src/hero_quant/skills/loader.py`
- Modify: `src/hero_quant/agent/context.py`
- Test: `tests/test_skill_disclosure.py`

**Step 1: Write the failing test**
```python
def test_skill_two_phase_disclosure(tmp_path):
    from hero_quant.skills.loader import SkillsLoader
    (tmp_path/"SKILL.md").write_text("---\nname: demo\n---\nbody")
    loader=SkillsLoader([str(tmp_path)])
    desc=loader.get_descriptions()
    assert "demo" in desc
    assert len(desc)<500
    content=loader.get_content("demo")
    assert "body" in content
```

**Step 2: Run test — confirm it fails** `pytest tests/test_skill_disclosure.py -v` FAIL ModuleNotFound
**Step 3: Write minimal implementation**
- `loader.py`: 5 roots分级 + `snapshot()` digest + `skill tool` 触发按需 `<skill_content>` 全文，`fs/observed` 同步失效

**Step 4: Run test — confirm it passes** `pytest tests/test_skill_disclosure.py -v` PASS
**Step 5: Commit** `git commit -m "feat(skills): 两段式披露+热失效"`

---

### Task B4: LangGraph 研究团队 Subagents

**Files:**
- Create: `src/hero_quant/agent/graph.py`
- Create: `src/hero_quant/agent/state.py`
- Test: `tests/test_graph_subagents.py`

**Step 1: Write the failing test**
```python
def test_graph_builds():
    from hero_quant.agent.graph import build_research_graph
    g=build_research_graph()
    assert g is not None
    assert hasattr(g,"invoke")
```

**Step 2: Run test — confirm it fails** `pytest tests/test_graph_subagents.py -v` FAIL
**Step 3: Write minimal implementation**
```python
# state.py TypedDict State + reducer
# graph.py StateGraph(plan→execute→verify)+create_agent叶子×N Subagents并行 + delegationDepth预算5
from langgraph.graph import StateGraph
def build_research_graph(): ...
```

**Step 4: Run test — confirm it passes** `pytest tests/test_graph_subagents.py -v` PASS
**Step 5: Commit** `git commit -m "feat(agent): LangGraph研究团队Subagents"`

---

### Task B5: 核心工具实体（15个）首批

**Files:**
- Create: `src/hero_quant/tools/market_data.py`
- Create: `src/hero_quant/tools/backtest.py`
- Create: `src/hero_quant/tools/quantlib_tool.py`
- Test: `tests/test_tools_entities.py`

**Step 1: Write the failing test**
```python
def test_tools_entities_registered():
    from hero_quant.tools.registry import TOOL_REGISTRY
    import hero_quant.tools.market_data, hero_quant.tools.backtest
    assert "get_market_data" in TOOL_REGISTRY
    assert "run_backtest" in TOOL_REGISTRY
```

**Step 2: Run test — confirm it fails** `pytest tests/test_tools_entities.py -v` FAIL
**Step 3: Write minimal implementation**
- 各 tool 用 `@tool(parameters={...}, output={...}, is_concurrency_safe=...)` 定义，`get_market_data` 走 registry，真调则审 `isConcurSafe`

**Step 4: Run test — confirm it passes** `pytest tests/test_tools_entities.py -v` PASS
**Step 5: Commit** `git commit -m "feat(tools): 首批15核心工具实体"`

---

## Wave C — 生产闭环（2周）

### Task C1: Backtest 正本（PIT正逻辑+多引擎+tearsheet）

**Files:**
- Modify: `src/hero_quant/backtest/validation.py`
- Modify: `src/hero_quant/backtest/engine.py`
- Test: `tests/test_backtest_pit_correct.py`

**Step 1: Write the failing test**
```python
def test_pit_correct_logic():
    from hero_quant.backtest.validation import validate, ValidationError
    import pandas as pd
    prices=pd.DataFrame({"close":[100,101]}, index=pd.date_range("2026-08-10",periods=2))
    # w>p 应抛（未来数据），w<p 应过
    try: validate(prices, weights_on="2026-08-11", price_date="2026-08-10")
    except ValidationError: pass
    else: assert False
    validate(prices, weights_on="2026-08-09", price_date="2026-08-10")
```

**Step 2: Run test — confirm it fails** `pytest tests/test_backtest_pit_correct.py -v` FAIL 旧反逻辑仍过
**Step 3: Write minimal implementation**
- `validate` 改 `ts_w > ts_p → ValidationError`，`engine.run` 产 `positions.csv/fills.csv/metrics.json` 多引擎占位，`tearsheet.html` 月热力占位

**Step 4: Run test — confirm it passes** `pytest tests/test_backtest_pit_correct.py -v` PASS
**Step 5: Commit** `git commit -m "fix(backtest): PIT正逻辑+引擎抽象"`

---

### Task C2: 问答与审批Ask卡片后端

**Files:**
- Create: `src/hero_quant/interaction/questions.py`
- Create: `src/hero_quant/interaction/approval.py` # 若A4已建则扩展
- Test: `tests/test_ask_card.py`

**Step 1: Write the failing test**
```python
def test_ask_card_blocks(tmp_path):
    from hero_quant.interaction.questions import UserQuestionService
    svc=UserQuestionService()
    # 模拟无provider → 抛 NO_PROVIDER，经 tool 层转为 ask 决议
    try: svc.ask_sync(questions=[{"id":"q1","question":"确认？","header":"Confirm","options":[{"label":"是","description":"推荐"}]}])
    except Exception as e: assert "NO_PROVIDER" in str(e)
```

**Step 2: Run test — confirm it fails** `pytest tests/test_ask_card.py -v` FAIL
**Step 3: Write minimal implementation**
- `questions.py`: `AskUserQuestionItem {id,question,header,options,multiSelect,intent}` + `ask()→provider.ask(signal)` + `BAD_INTENT/DELEGATED_CALLER` 校验；`approval.py` 复用A4的 `ask→guard` 三段式
**Step 4: Run test — confirm it passes** `pytest tests/test_ask_card.py -v` PASS
**Step 5: Commit** `git commit -m "feat(interaction): Ask卡片+interrupt"`

---

### Task C3: 优雅降级（RetryPolicy+error_handler Saga）+ 成本熔断

**Files:**
- Create: `src/hero_quant/agent/policies.py`
- Modify: `src/hero_quant/agent/graph.py`
- Test: `tests/test_policies.py`

**Step 1: Write the failing test**
```python
def test_retry_and_budget_breaker():
    from hero_quant.agent.policies import RetryPolicy, BudgetBreaker
    rp=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
    assert rp.should_retry(ConnectionError("x"), attempt=1) is True
    bb=BudgetBreaker(daily_limit=5.0)
    assert bb.should_fallback(cost=6.0) is True
```

**Step 2: Run test — confirm it fails** `pytest tests/test_policies.py -v` FAIL
**Step 3: Write minimal implementation**
- `policies.py`: `RetryPolicy` 指数退避+jitter + `error_handler(state,NodeError)->Command goto compensate` Saga；`BudgetBreaker` 滑动窗口cost熔断

**Step 4: Run test — confirm it passes** `pytest tests/test_policies.py -v` PASS
**Step 5: Commit** `git commit -m "feat(agent): 优雅降级+成本熔断"`

---

### Task C4: 心跳四层 + 熔断双桶

**Files:**
- Create: `src/hero_quant/telemetry/heartbeat.py`
- Create: `src/hero_quant/telemetry/circuit.py`
- Test: `tests/test_heartbeat_circuit.py`

**Step 1: Write the failing test**
```python
def test_heartbeat_and_circuit():
    from hero_quant.telemetry.heartbeat import HeartbeatTimer
    import threading, time
    fired=[]
    with HeartbeatTimer("t", interval=0.1, emit=lambda e: fired.append(e)):
        time.sleep(0.35)
    assert len(fired)>=2
    from hero_quant.telemetry.circuit import CircuitBreaker
    cb=CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
    for _ in range(5): cb.record_failure()
    assert cb.state=="OPEN"
```

**Step 2: Run test — confirm it fails** `pytest tests/test_heartbeat_circuit.py -v` FAIL
**Step 3: Write minimal implementation**
- `heartbeat.py`: `threading.local+_set_emitter` + `HeartbeatTimer(max(0.5,interval)) daemon+join(1.0)` 双看门狗（write仅warn/read熔断）
- `circuit.py`: `CLOSED→OPEN(HalfOpen)` `failure 50%/slow 50%/TIME 30s/open30s/half5`
**Step 4: Run test — confirm it passes** `pytest tests/test_heartbeat_circuit.py -v` PASS
**Step 5: Commit** `git commit -m "feat(telemetry): 心跳四层+熔断"`

---

### Task C5: 断点续跑（PostgresSaver+Temporal占位）

**Files:**
- Create: `src/hero_quant/checkpoint/postgres.py`
- Create: `src/hero_quant/checkpoint/temporal.py`
- Test: `tests/test_checkpoint.py`

**Step 1: Write the failing test**
```python
def test_checkpoint_roundtrip():
    from hero_quant.checkpoint.postgres import get_saver
    saver=get_saver(dsn="memory://test")
    tid="backtest:1:tenantA"
    saver.put(tid, {"step":1}, {"next":"plan"})
    assert saver.get(tid)["step"]==1
```

**Step 2: Run test — confirm it fails** `pytest tests/test_checkpoint.py -v` FAIL
**Step 3: Write minimal implementation**
- `postgres.py`: `AsyncPostgresSaver(ConnectionPool)+setup()+thread_id三段式+TTL`；`temporal.py`: `Activity heartbeat 15s + heartbeatDetails续跑` 占位
**Step 4: Run test — confirm it passes** `pytest tests/test_checkpoint.py -v` PASS
**Step 5: Commit** `git commit -m "feat(checkpoint): PostgresSaver+Temporal占位"`

---

### Task C6: 幂等去重表（Idempotency Ledger）

**Files:**
- Create: `src/hero_quant/governance/dedup.py`
- Test: `tests/test_dedup.py`

**Step 1: Write the failing test**
```python
def test_idempotency_ledger(tmp_path):
    from hero_quant.governance.dedup import DedupStore
    s=DedupStore(str(tmp_path/"dedup.db"))
    k="tenant:wf1:step2:run_backtest:600519"
    assert s.insert_pending(k,"run_backtest") is True
    assert s.insert_pending(k,"run_backtest") is False
    s.mark_success(k, {"ok":True})
    assert s.get(k)["status"]=="SUCCESS"
```

**Step 2: Run test — confirm it fails** `pytest tests/test_dedup.py -v` FAIL
**Step 3: Write minimal implementation**
- `dedup.py`: `tool_call_dedup(idempotency_key PK,status PENDING|SUCCESS|FAILED,result,error) INSERT ON CONFLICT WAIT` + `key={tenant}:{workflowId}:{stepId}:{tool}:{businessId}` 编排层派生
**Step 4: Run test — confirm it passes** `pytest tests/test_dedup.py -v` PASS
**Step 5: Commit** `git commit -m "feat(governance): 幂等去重表"`

---

### Task C7: 前端重做（tearsheet+live监控）

**Files:**
- Modify: `frontend/src/pages/Research.tsx`
- Modify: `frontend/src/pages/Chat.tsx`
- Create: `frontend/src/pages/Monitor.tsx`
- Test: `frontend/src/__tests__/Research.test.tsx`

**Step 1: Write the failing test**
```tsx
// Research.test.tsx
import {render, screen} from "@testing-library/react"
import Research from "../pages/Research"
test("research renders tearsheet", ()=>{render(<Research/>); expect(screen.getByText(/本月收益热力|累积收益/)).toBeInTheDocument()})
```

**Step 2: Run test — confirm it fails** `npm run test:run` FAIL
**Step 3: Write minimal implementation**
- `Research.tsx` 接 `positions.csv/metrics.json/tearsheet.html` 真渲染（ECharts月热力+回撤topN），`Chat.tsx` 加 `grounding·已校验` 徽标+tool轨迹，`Monitor.tsx` `events.jsonl offset` 实时 SSE + OTel cost 熔断条

**Step 4: Run test — confirm it passes** `npm run test:run` PASS
**Step 5: Commit** `git commit -m "feat(frontend): tearsheet+live监控"`

---

### Task C8: 安全收口（Cerbos PDP占位+Presidio+Store隔离）

**Files:**
- Create: `src/hero_quant/security/policy.py`
- Modify: `src/hero_quant/memory/store.py`
- Test: `tests/test_policy_store.py`

**Step 1: Write the failing test**
```python
def test_store_tenant_isolation(tmp_path):
    from hero_quant.memory.store import MemoryStore
    a=MemoryStore(tmp_path/"a"); b=MemoryStore(tmp_path/"b")
    a.write("k","hello tenant A")
    assert b.search("hello")==[]
```

**Step 2: Run test — confirm it fails** `pytest tests/test_policy_store.py -v` FAIL store隔离未生效或未前缀
**Step 3: Write minimal implementation**
- `policy.py`: `Cerbos PDP sidecar` 占位 `resource: quant.strategy actions:[backtest:run,live:deploy] condition: notional<limit` + `Presidio anonymizer` + `Store (tenant,thread) namespace` 隔离
**Step 4: Run test — confirm it passes** `pytest tests/test_policy_store.py -v` PASS
**Step 5: Commit** `git commit -m "feat(security): PDP+Presidio+多租户Store"`

---

### Task C9: E2E收口与性能回归

**Files:**
- Create: `tests/test_e2e_full.py`
- Test: `tests/test_e2e_full.py`

**Step 1: Write the failing test**
```python
def test_e2e_full_to_report(tmp_path):
    from hero_quant.agent.graph import build_research_graph
    g=build_research_graph()
    # 端到端：LangGraph图+真registry(synthetic)+真backtest+HITL mock
    res=g.invoke({"messages":[{"role":"user","content":"回测 600519.SH 近一月等权"}]}, config={"configurable":{"thread_id":"e2e-1"}})
    assert res is not None
```

**Step 2: Run test — confirm it fails** `pytest tests/test_e2e_full.py -v` FAIL graph未接pipeline
**Step 3: Write minimal implementation**
- 串联 `registry→grounding→engine→tearsheet` helper `run_e2e`，`answer` 带 `metrics+provenance+manifest_hash`

**Step 4: Run test — confirm it passes** `pytest tests/test_e2e_full.py -v` PASS
**Step 5: Commit** `git commit -m "feat: e2e full LangGraph闭环"`

---

## 执行交接

Plan saved to `docs/plans/2026-08-20-hero-quant-v2.md`. Two execution options:

1. **Subagent-Driven** — 我按 task A1→C9 依次 `sessions_spawn` 派子代理（每任务 TDD + 两阶段 review），你只需等待。
2. **Manual** — 你按本计划自跑，每完成一 task 贴测试结果我来 review。

Which approach?
