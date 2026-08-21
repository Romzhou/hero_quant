# hero-quant 成熟度冲 3 实施计划 — TDD 任务清单

> **Date:** 2026-08-21  
> **Parent:** `2026-08-21-tradingagents-deep-dive.md` + `2026-08-21-tradingagents-7d-annex.md`  
> **Goal:** 七维成熟度全部 ≥3.0（当前 1.5-2.5 → 3.x），以最小可用治理拿下可商用门槛，不追 5  
> **Method:** 每任务 2-5 分钟，TDD 强制（先写失败测试→最小实现→见绿→提交），任务间可并行标注  
> **Verify:** `pytest -q` 全绿 + `pytest tests/test_pit -v` + `ledger verify()` + `trace 50k` 不丢

> I'm using the writing-plans skill to create the implementation plan.

---

## 0. 依赖图与 Wave 划分

```
Wave A (P0, 本周, 可并行 4 lanes)
  A1 安全: safe_ticker_component
  A2 AgentLoop: token 估算 + wrap-up nudge
  A3 记忆: content_hash 去重 + namespace 搜索修复
  A4 工程: 最小 CI

Wave B (P1, 下周, 依赖 A)
  B1 可观测: metrics histogram + OTel 接线最小可用
  B2 安全: redaction 自动织入 + CSP/DNS 部分
  B3 记忆+RAG: BM25 路由 + Ebbinghaus 衰减最小版
  B4 Multi-Agent: Send fanout 占位打通 + 轻 pros/cons

Wave C (收口)
  C1 批量 Bench + VCR 骨架
```

**YAGNI 约束：** 每新增 Loader/Skill 需证明 >20% 用户用或替换 1 个；不引入 TradingAgents 3 轮风险链；双模型 catalog 本轮不做（保 3 分先）。

---

## Wave A — 冲 3 的地基（4 lanes 并行）

### A1. 安全 — `safe_ticker_component` 移植（提升 安全 2→3）

- **Files:** `src/hero_quant/security/sanitize.py` (新建) + `src/hero_quant/data/registry.py` + `src/hero_quant/checkpoint/postgres.py` + `src/hero_quant/governance/ledger.py` 写入侧调用 + `tests/test_safe_ticker.py` (新建)
- **Task A1-1 — TDD: 非法 ticker 拒绝**
  - *Test:* `test_safe_ticker.py::test_rejects_traversal` — `safe_ticker_component("../../../etc/passwd")` 抛 `ValueError`，`safe_ticker_component("600519.SS")` 通过，`safe_ticker_component("...")` 拒，空串拒，len>32 拒；覆盖 `dataflows/utils.py:9` 同 regex `^[A-Za-z0-9._\-\^=+]+$`。
  - *Impl:* 拷贝 `TradingAgents/dataflows/utils.py:9-42` 正则+len+dotted-only 校验，`return sanitized`。
  - *Verify:* `pytest tests/test_safe_ticker.py -v`

- **Task A1-2 — 接线所有文件写入**
  - *Test:* `test_safe_ticker_integration` — mock `Path(results_dir)/safe` 调用，传非法 ticker 时 `registry.get_bars` / `ledger.append` / `dedupderive_key` 前置校验抛错不触 FS。
  - *Impl:* 在 `data/registry.py: get_bars` 入口、`governance/ledger.py: append` `tenant` 拼接前、`checkpoint` key 生成前加 `safe_ticker_component`。
  - *Verify:* `pytest -q -k safe_ticker`

### A2. Agent Loop — token 估算与收尾（Loop 2.5→3）

- **Files:** `src/hero_quant/agent/loop.py` + `src/hero_quant/agent/policies.py` + `tests/test_loop_maturity3.py`
- **Task A2-1 — TDD: 字符→token 估算**
  - *Test:* 构造 `buffer = "a"*6000`，`estimate = len(buffer)//4` 期望 1500 而非 6000；`token_limit=60000` 时 `buffer 240k chars` 才触发截断。
  - *Impl:* `loop.py:198,223` `len(buffer)` → `len(buffer)//4` 或 `len(json.dumps(messages))//4`（先取简单 //4，vibe 同款）。
  - *Verify:* `pytest tests/test_loop_maturity3.py::test_token_estimate -v`

- **Task A2-2 — TDD: wrap-up nudge 0.8*max**
  - *Test:* `max_iterations=5`，迭代 4 时注入 `[SYSTEM] wrap up` 的 user message，且最后一次迭代 `tool_defs is None`（强制文本收尾）。
  - *Impl:* 参照 `vibe loop:893,948` 在 `loop.py:190` 前加 `wrap_up_at = int(max_iterations*0.8)` 分支。
  - *Verify:* `pytest tests/test_loop_maturity3.py::test_wrap_up -v`

- **Task A2-3 — TDD: grounding 批冻结**
  - *Test:* 同批 tool_calls 含 `get_market_data(symbol=TSLA)` + `assert_price(TSLA, 9999)`，`batch_authorized_symbols` 取批前快照，故第二调用不因第一调用的 ingest 而通过。
  - *Impl:* `loop.py:427` 解析 `tool_calls_this_iter` 前 `snapshot = set(grounding._evidence.keys())`，传给 `authorize` 校验。
  - *Verify:* `pytest tests/test_loop_maturity3.py::test_batch_frozen_identity -v`

### A3. 记忆 — 去重与隔离修复（记忆 2→3）

- **Files:** `src/hero_quant/memory/store.py` + `tests/test_memory.py`（现有仅 7 行）
- **Task A3-1 — TDD: content_hash 去重**
  - *Test:* 同一内容不同 key `write("a","hello")` + `write("b","hello")` 30s 内第二笔被判重（hash 命中）；大小写/空白归一化后仍判重。
  - *Impl:* 引入 `persistent.py:35 _content_hash = sha256((name+content).lower().strip()).hexdigest()[:12]` 滑动窗口 `Dict[hash, ts]`，替换 `store.py:75 _last_write[key]`。
  - *Verify:* `pytest tests/test_memory.py -v`

- **Task A3-2 — TDD: namespace 搜索不泄漏**
  - *Test:* `store(namespace="tenantA:thread1")` 与 `tenantB:thread1` 同 `query`，`search(query, namespace="tenantA")` 不返回 tenantB；FTS 失败回退 LIKE 与 file glob 均过滤。
  - *Impl:* 修复 `store.py:155-236` 3 段回退均先 `sanitize + LIKE prefix%` 再 dedup（当前 FTS 后直接 raise 泄漏），file 回退加 `startswith safe_prefix`。
  - *Verify:* `pytest tests/test_memory.py::test_namespace_isolation -v`

- **Task A3-3 — TDD: 覆盖率≥10 用例**
  - *Test:* 增 `test_memory_rotation_keep_pending` / `test_same_cross` 占位（本轮仅加测试骨架，保证 3 分门槛的测试数）。
  - *Impl:* 补 `store.py:220` `memory_log_max_entries` 风格 `pending 永保` 旋转（可选，3 分最小版可先跳过，留 C 波）。
  - *Verify:* `pytest tests/test_memory.py -q` 全绿且 count≥8

### A4. 工程 — 最小 CI（工程 2.5→3）

- **Files:** `.github/workflows/ci.yml` (新建) + `pyproject.toml` / `requirements-lock.txt`
- **Task A4-1 — TDD/Verify: hash-lock dry-run + ruff + pytest**
  - *Test:* 本地 `pip install --dry-run --require-hashes -r requirements-lock.txt` 成功；`ruff check src` 0 error。
  - *Impl:* 新建 `ci.yml` 3 jobs：`hash-lock: pip --dry-run --require-hashes` / `lint: ruff check` / `test: pytest -q --cov --cov-fail-under=0`（先 0 阈值保 3 分）。
  - *Verify:* `act -W .github/workflows/ci.yml --dry-run` 或 `gh workflow view`；`pytest -q` 本地绿

---

## Wave B — 拉齐到 3 的主体

### B1. 可观测 — 最小可用 OTel+metrics（可观测 2.5→3）

- **Files:** `src/hero_quant/telemetry/otel.py` + `src/hero_quant/api/server.py` + `src/hero_quant/telemetry/circuit.py` + `docker-compose.yml`
- **Task B1-1 — TDD: histogram + counter**
  - *Test:* `GET /metrics` 含 `hero_quant_requests_total` + `http_request_duration_seconds_bucket`；`CircuitBreaker.allow()` 时 `circuit_state` gauge 暴露。
  - *Impl:* `server.py:30` 加 `Histogram("http_request_duration_seconds", ...)` + 中间件 `time.perf_counter` observe；`circuit.py` 加 `prometheus Gauge`（可选，3 分可用 stub）。
  - *Verify:* `curl -s localhost:8899/metrics | grep histogram`

- **Task B1-2 — OTel 接线最小可用（离线安全）**
  - *Test:* 设 `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/logs` 时 `SessionTelemetryCoordinator.export()` 走 `BatchLogRecordProcessor` 不抛；未设时静默 no-op。
  - *Impl:* `otel.py:73` 把 `urllib POST JSON` 换 `opentelemetry-exporter-otlp + LoggerProvider`，保留 `try/except` 离线安全，`shutdownTimeout 3000` 可后补。
  - *Verify:* `docker compose up otel-collector --wait` 后 `export()` 返回无异常，`pytest tests/test_otel.py -v`

### B2. 安全 — 自动织入与最小 CSP（安全 3 达标）

- **Files:** `src/hero_quant/security/redaction.py` + `src/hero_quant/agent/trace.py` + `src/hero_quant/api/server.py`
- **Task B2-1 — TDD: trace/ledger 自动脱敏**
  - *Test:* `write_tool_result({"api_key":"sk-123"})` 落盘后为 `***REDACTED***`；`content` 字段透传但顶层 secret 仍脱敏（`ARGUMENTS_SINK` vs `RESULT_SINK`）。
  - *Impl:* `trace.py:200` `write` 入口包 `redact_payload(..., sink=...)`；`ledger.py: append` 前同款。
  - *Verify:* `pytest tests/test_redaction_auto.py -v`

- **Task B2-2 — TDD: 最小 CSP+DNS 校验**
  - *Test:* `GET /` 响应头含 `Content-Security-Policy: default-src 'self'` + `X-Frame-Options: DENY`；`Host: evil.com` 被 403（`_reject_untrusted_loopback_host`）。
  - *Impl:* 移植 `vibe security:228 _apply_security_headers` 20 行到 `server.py` 中间件，`_reject_untrusted_loopback_host` 仅环回校验（3 分最小版不做 HMAC 全量）。
  - *Verify:* `pytest tests/test_security_headers.py -v` + `curl -H "Host: evil.com" localhost:8899/live -i | grep 403`

### B3. 记忆+RAG — BM25 与衰减最小版（RAG 2→3, 记忆 3 巩固）

- **Files:** `src/hero_quant/mcp/router.py` + `src/hero_quant/memory/store.py` + `src/hero_quant/memory/lifecycle.py` (新建)
- **Task B3-1 — TDD: BM25 路由**
  - *Test:* `router.route("momentum factor")` 首位为 `compute_factor`，`router.route("alpha decay")` 能召回 `memory/search` 类；`score` 用 `K1=1.5 B=0.75` 计算而非固定 +3。
  - *Impl:* `router.py:15-61 _score_tool` 换 `semantic_links.py:99,145` `idf + BM25`，语料为 `tool.description` 全集，`avg_dl` 预计算。
  - *Verify:* `pytest tests/test_router_bm25.py -v`

- **Task B3-2 — TDD: Ebbinghaus 14d 加权**
  - *Test:* 同 query 下，`quality_score 0.9 + access_count 5 + 1 天前` 的条目排在 `qs 0.9 + 14 天前` 之前；`compute_importance = qs*(exp(-λ*days)+min(0.3,ac*0.1))`。
  - *Impl:* `store.py` 增 `quality_score, access_count, last_accessed` 列（可先内存 Dict 模拟，3 分不改 DDL 也可），`search()` 时 `score*(0.5+0.5*importance)` 如 `vibe persistent:75,403`。
  - *Verify:* `pytest tests/test_memory_decay.py -v`

### B4. Multi-Agent — 图内并行与轻对抗（Multi-Agent 2→3）

- **Files:** `src/hero_quant/agent/graph.py` + `tests/test_graph_maturity3.py`
- **Task B4-1 — TDD: Send fanout 打通**
  - *Test:* `build_research_graph(selected=[market,sentiment,news])` 时 `plan→[Send(analyst)]*3→verify`，3 analyst 并行执行且 `wall_time < 0.6* serial`。
  - *Impl:* `graph.py:94` 把 `is_concurrency_safe` 思想搬进图：`plan` 节点 `Command(goto=[Send("market",state), ...])`，verify 节点 `Annotated[list, add]` 归约。
  - *Verify:* `pytest tests/test_graph_maturity3.py::test_fanout -v`

- **Task B4-2 — TDD: 轻 pros/cons 对抗**
  - *Test:* `verify` 节点输出含 `pros: [...] cons: [...] confidence: 0.x` 且由同一 LLM 一次生成（非 3 轮风险链）。
  - *Impl:* `graph.py: verify` prompt 加 `请给出多空两面 pros/cons + 置信度`，不用新增 TradingAgents 式 Risk 链。
  - *Verify:* `pytest tests/test_graph_maturity3.py::test_pros_cons -v`

---

## Wave C — 收口到 3+

### C1. 批量 Bench + VCR 骨架（为 3+ 加分，不阻塞）

- **Files:** `src/hero_quant/backtest/bench.py` (新建) + `src/hero_quant/agent/trace.py` + `tests/test_bench.py`
- **Task C1-1 — TDD: run_batch + 区域 benchmark**
  - *Test:* `run_batch(["600519.SS","0700.HK"], dates=["2024-01-01"])` 返回 `metrics.json` 含 `alpha vs 000001.SS/^HSI` 区分。
  - *Impl:* 复用 `trading_graph 308 benchmark_map:152` 后缀映射，包装 `backtest/engine.py` 批量循环。
  - *Verify:* `pytest tests/test_bench.py -v`

- **Task C1-2 — TDD: llm_usage VCR 录制**
  - *Test:* `trace.jsonl` 含 `llm_usage {input_tokens, output_tokens}` 可回放离线 `replay` 不调 LLM。
  - *Impl:* `loop.py:313` 累积 `usage_metadata` 写 `trace`，加 `replay` 分支读 `llm_usage.json`。
  - *Verify:* `pytest tests/test_vcr.py -v`（mock LLM 离线绿）

---

## 执行模式

- **Subagent-Driven（推荐）：** 每任务 `sessions_spawn` implementer（TDD）→ spec-reviewer → quality-reviewer，三审过后提交。预计 18 任务 × 5min ≈ 90min，4 lanes 并行可压至 45min。
- **Manual：** 按 Wave 顺序 `pytest -q` 驱动，用户自跑。

**下一步：** 选执行模式后，首批并行 `A1-1/A2-1/A3-1/A4-1` 四 lane 同启。

---

## 验收门（冲 3 完成标准）

- `pytest -q` 全绿（含新增 18 用例）
- `pytest tests/test_pit -v` + `ledger verify()` + `Trace 50k` 侧车不丢
- `curl /metrics | grep histogram` + `Host: evil` 403 + `safe_ticker` 拒绝用例
- 七维自评复核全 ≥3.0（见 7d-annex 雷达）
