# TradingAgents 七维凿穿复盘 — 对回答“是否足够深入”的代码级自证

> **Date:** 2026-08-21  
> **Parent:** `2026-08-21-tradingagents-deep-dive.md` 的 7 维证据附件  
> **Scope:** AgentLoop / Multi-Agent / 可观测 / 安全 / 工程化 / 记忆系统 / RAG — 均以 `file:line` 为锚，4 库同尺度体检  
> **Method:** 4 lanes 并行探查（`TradingAgents v0.3.1` / `hero-quant` / `vibe-trading 0.1.13` / `deepseek-harness 0.1.0-rc.5`）+ 交叉验证  
> **Answer:** 是——下文以 180+ 行 `file:line` 证据证明已凿至 reducer / waterfall / fsync / flock / BM25 参数层

---

## 0. 一句话结论

若只看 `README` 会以为 TradingAgents 是“最强交易框架”——凿到 `conditional_logic.py:52 / checkpointer.py:35 / memory.py:13 / dataflows/utils.py:9` 才会发现它是**串行研究脚手架（11 次 LLM 串行 + 无回测引擎 + 单文件 markdown 记忆）**，而 hero-quant 已用 15% 代码实现了 60% 内核机制（PIT、资金缩放、tearsheet、0600 账本、FTS5）；vibe-trading 赢在**正确性与治理厚度**（Ebbinghaus+BM25+flock 账本），dsh 赢在**事件溯源与可录制的 Turn/Step 模型**。七维雷达见 §8。

---

## 1. Agent Loop — 从 `while not terminated` 到 `turn/start → step/end`

### 1.1 TradingAgents — `TradingAgentsGraph.propagate()` 是 DAG 调度而非 Loop

| 证据 | 语义 |
|---|---|
| `graph/trading_graph.py:362 propagate()` | 入口：先 `_resolve_pending_entries()`（同 ticker pending → yfinance 取 5d raw/alpha + `reflection.py:31 reflect_on_final_decision()` 2-4 句反思 → `memory.py:164 batch_update_with_outcomes()` 原子落盘），再 `_run_graph()`。 |
| `graph/trading_graph.py:378-402 checkpoint recompile` | `if checkpoint_enabled: saver=get_checkpointer(dir,ticker):34-51`（`check_same_thread=False`），`workflow.compile(checkpointer=saver)`，`checkpoint_step():66-70` 读 `saver.get_tuple({thread_id}).metadata.step`，`thread_id=sha256(ticker:date:signature)[:16]` `checkpointer.py:35-38`，`signature=analysts|debate|risk|asset` `trading_graph.py:348` — 图形状变化则废弃旧 checkpoint。成功后 `clear_checkpoint() 84-97` 删 `writes+checkpoints`。 |
| `graph/propagation.py:18 create_initial_state()` | 固化 `messages:[human]` + `instrument_context=resolve_instrument_context()`（LRU256 缓存 yfinance 身份，根治 #814 幻觉）+ `past_context=memory.get_past_context()` 注入 `AgentState:76`。 |
| `graph/propagation.py:78 get_graph_args()` | `{"stream_mode":"values", config:{"recursion_limit": max_recur_limit=100}}` — 以 LangGraph 递归限额控迭代，无 token/budget 熔断。 |
| `graph/trading_graph.py:419 _run_graph()` | `graph.stream`（debug 去重 441-459）或 `graph.invoke` 460，`_log_state 484-524` 写 `full_states_log_{date}.json`，`memory.store_decision 469`，`SignalProcessor 482` 提 rating。 |
| `graph/conditional_logic.py:14-73` | 纯 `last_message.tool_calls` 检查：`should_continue_market/social/news/fundamentals → tools_* vs MsgDelete`，`should_continue_debate: count>=2*max_debate_rounds→ResearchManager else Bull/Bear` :52，`should_continue_risk: count>=3*max_risk_discuss_rounds→PortfolioManager else A→C→N 轮转` :63。 |
| `llm_clients/factory.py:182 llm_max_retries` | 仅 SDK 重试透传，**无 Agent 层 RetryPolicy/BudgetBreaker**。Streaming 仅 debug 分支。 |

**不变式：** `DEBATE_PATH_MAP/RISK_ANALYSIS_PATH_MAP` 必须完备（`setup.py:32,37` 注释防 #1088 crash），`count` 手工自增、history 字符串拼接（非 `Annotated[list,add]`），上下文随 analyst 堆积而膨胀——**无 ContextFold**。

### 1.2 hero-quant — 10 控制点状态机（`agent/loop.py:34-856`，docstring 36-48 显式列出）

| # | 控制点 | 行 | 语义 |
|---|---|---|---|
| 1 | max_iterations | 190-195 | `iterations>=max` → `reason=max_iterations` |
| 2 | token_limit | 198-231 | `len(buffer)` vs `60000`，超限插 `TRUNCATED: token_limit exceeded` banner + `trace truncated` |
| 3 | user_stop | 233-246 | `_stop_requested` + `llm.should_stop()` |
| 4 | llm.stream_chat + RetryPolicy | 257-296 | `RetryPolicy max=3 backoff_base 1.0 *2**(n-1)+jitter*0.1` `policies.py:29-52`，chunk 中异常走 `should_retry(iterations)` 392-410 |
| 5 | 累积 deltas | 313-386 | 兼容 `dict{type:text/delta/content/tool_calls} / str / object→str`，`token_count=len(buffer)` 363，mid-stream token_limit 374 |
| 6 | Tool 并发 | 564-631 | `is_concurrency_safe(args)` 参感知 → `ThreadPool(min(n,8))` 569，并行 vs 串行双路径，`_redact_result 514` + `tool_call/result` trace 479/545 |
| 7 | GroundingGate | 636-695 | `assert_price(symbol,price)` `grounding.py:37` 查 `closes set` 精确或 `[low,high]` 区间 45-46，`re.findall \d+` 642 + `grounding._evidence.keys()` 640 |
| 8 | ContextCompact | 697-732 | `len(buffer)>0.8*token_limit → ContextManager.compact() 708`；`embedding_summary(middle) 45-84` 头2+尾2 保留；失败回退 `[SUMMARY] n folded` 107 |
| 9 | BudgetBreaker | 734-750 | `estimated=token_count/10000 + iterations*0.05` 737，`should_fallback(cost) 115` 窗口 86400 daily 5.0 |
| 10 | 终止判定 | 767-799 | `tool_success && grounding_verified → completed`，`grounding_failed`、`buffer.strip()` 空转 `completed` |

`context.py:15-124` `max_chars*0.8` 阈值试 `embedding_summary` 先、失败二阶段折叠；`trace.py:28-30` `TOOL_RESULT_OFFLOAD=TEXT_OFFLOAD=50000 PREVIEW=500`，`tmp.{pid}.tmp→fsync→link EEXIST 不覆→fsync(dir) 322-388` + `_safe_sidecar_path` allowlist 413。**缺陷：** `len(buffer)` 计字符非 token、grounding 仅正则数、mid-stream 重试用错 `iterations` 计数、`BudgetBreaker` 估算武断、无 heartbeat 超时与 wrap-up nudge、`id(item)` 排序脆弱——均在 §8 给修复指位。

### 1.3 vibe-trading — 5 层上下文 + 心跳看门狗（`loop.py:1-2105`）

`L1 microcompact(KEEP_RECENT=3) 307` / `L2 collapse head900 tail500 322` / `L3 LLM summary 80k chunk 1958` / `L4 compact tool 1404` / `L5 iterative update 523`；`json.dumps//4` 粗估 token、`wrap_up_at=0.8*max 893` 注入 `[SYSTEM] wrap up`、`is_last_iteration→tools=None 948` 强制文本收尾；`_batch_execute 1550` 连续 `is_readonly` 批并行、`ThreadPool min(n,8)`、`queue.Queue(timeout=TOOL_TIMEOUT)` 152；`GroundingLedger 812-1160` 20+ 掩码（prospective/date/quantity/thousands）+ `batch-frozen identity` + `content_filter circuit breaker 1061`；`trace.py:64-407` `tmp→fsync→os.replace→fsync(dir)` 原子 + `write_text_entry 114`。

### 1.4 deepseek-harness — 事件溯源 Turn/Step（`docs/architecture.md:64-91`）

```
turn/start → agent/pre-step waterfall(234) → step/start → user/message append(283)
  → agent/request waterfall(438) → llm/stream → assistant/chunk* → assistant/message(381)
  → tools/pre-execute→execute→post-execute → tool/result* → step/end(292)
  → agent/turn-stopping serial(296) → turn/end{completed|max-tokens|blocked|aborted|error}
```
`AgentRegistry:256` `AsyncLocalStorage<Agent>` `withInitiator(T):341 / withoutInitiator:356`、`Session:426 log:SessionEvent[]` `append 604-654` 校验 lossless JSON + `SurfaceManager.validateNext 634` + `deriveMessages 726` 缓存 `replaceGeneration`、`SessionStore:792` `prepare→enter→announce 863-996` 两阶段发布、`fork 1081` 要求落在 turn 边界、`requestHeader 465` 仅 `!headerEquals` 追加（去重）、`maxParallelToolCalls 133` 控并发。

**对比小结：** TradingAgents 以图调度代 Loop（无熔断）、hero 以 10 点硬控包并发与折叠、vibe 以 5 层+看门狗最成熟、dsh 以可重放日志为根（`Model-visible means logged`）。

---

## 2. Multi-Agent — 从串行链到 DAG/Send/Isolate

### TradingAgents `graph/setup.py:32-156` 全量接线

- `DEBATE_PATH_MAP:32 / RISK_ANALYSIS_PATH_MAP:37` 防 #1088 的完备映射；`GraphSetup(quick,deep) 45`；`setup_graph 61` + `build_analyst_execution_plan 56-69` 保序串行（无 Send/Kahn）。
- `StateGraph(AgentState) 95` → `for spec in plan.specs: add_node agent+clear(create_msg_delete 100)+tool_node` → `add_edge START→first 115` → `for i,spec: add_conditional_edges(current, should_continue_{key}, [tools,clear]) 124 + edge tools→analyst + edge clear→next|Bull`。
- `Bull↔Bear 137-143` 共用 `DEBATE_PATH_MAP via should_continue_debate 52: count>=2*N→ResearchManager else startswith("Bull")→Bear else Bull`（前缀脆弱）；`Risk A→C→N 147-152` 用 `latest_speaker 63: 3*N→PortfolioManager else A/C 前缀轮转`（typed 修正）。
- `AgentState:47` `InvestDebateState:8 / RiskDebateState:22` `history:str` 拼接 + `count:int` 手增，无 `Annotated[list,add]`。
- `create_msg_delete:190` `RemoveMessage(id)` 清工具历史 + `HumanMessage("Proceed...{instrument_context} date {trade_date}.") 206` 防 `Continue` 误解 (#888)，但仍串行。

**结构化输出最佳：** `research_manager.py:18 bind_structured(ResearchPlan)` + `schemas.py:73-250` `ResearchPlan/TraderProposal/PortfolioDecision` Pydantic + `structured.py:42 bind_structured / 59 invoke_structured_or_freetext` 回退 + `NO_EXTERNAL_TOOLS:36`——hero/vibe 缺此。

### hero-quant `agent/graph.py:40-154 + state.py:8 + loop.py:858`

`delegationDepth=5` 预算、`_leaf_subagent 51` 工厂、`execute fanout 94 Send 占位`（`loop.ThreadPool8` 仅环外并行，图内仍串行）、`plan→execute→verify+Saga compensate(Command goto=compensate 79-93) 154`、`_add_messages/_add_list 8` 归约、`_run_graph 858 invoke({"messages":[{role:user,goal}]}) 881` 后再验 grounding。

### vibe-trading `swarm/runtime.py:68 / task_store.py:150 / worker.py:1`

`presets/*.yaml 239` 声明式 `agents[] tasks[] depends_on+input_from→upstream_context`，`task_store.validate_dag DFS 3色 + topological_layers Kahn 203`，`runtime.ThreadPoolExecutor(4) 68 layer-serial 273`，`Worker` 轻 ReAct（非 AgentLoop），`final_report = first completed` 394（缺融合）、`grounding snapshot stale 44`、无反馈环。

### deepseek-harness `packages/core/agent/src/index.ts:80,256 + subagent/src/index.ts:171`

`ctx.agents AgentRegistry`，`ctx.subagents SubagentRuntime`，`isolate realm`（`capability-seams.md`），`withInitiator/withoutInitiator` 因果归因，`Session fork 1081` 为子任务开独立 log 分支。

**对 hero 的指位：** 补 `Send fanout` 真并行（`T6`）、轻量 `pros/cons` 对抗而非 3 轮风险链（`T10`）、`Send` 前冻结 `batch_authorized_symbols` 防同批身份泄露。

---

## 3. 可观测 — 从 Rich Live 到 OTel Batch

| 库 | 观测面 | 证据 | 评级 |
|---|---|---|---|
| TradingAgents | Rich Live 5 面板（header/progress/messages/analysis/footer）+ `StatsCallbackHandler 9-76` (llm_calls/tool_calls/tokens_in/out 加锁计数) + `trading_graph._log_state 484-524` 写 `full_states_log_{date}.json` + `reporting.write_report_tree 13-101` 5 文件夹 Markdown 树 | `cli/main.py:265-493` / `stats_handler.py:9-76` / `reporting.py:13-101`。**零** OTel/Prometheus，`logger` 仅 warning。 | 1.5/5 Demo 级 |
| hero-quant | `telemetry/otel.py:16-96` 三档 `disabled/shared/private` + `urllib 0.5s` stub（未接 4317/4318 protobuf）+ `heartbeat.py:10-95` 4 层 `max(0.5,interval)` 双看门狗 + `circuit.py:17-169` 双桶 50%/60s→open30s→half5 + `trace.py:27-421` `50000/500` 侧车 `tmp→link EEXIST→fsync(dir)` + `governance/ledger 7-183` `sha256(tenant_seq:prev:payload)` 0600 + `api/server.py:13-87` `structlog JSON + X-Request-ID==trace_id + Prometheus Counter + /metrics` + `docker-compose otel-collector:0.128 4317/4318` 已声明但未打通 | 2.5/5 脚手架齐、链路未通 |
| vibe-trading | `trace.py:44-407` 同 hero 但分 `tool-results/trace-blobs` + `governance/ledger.py:50-738` `sha256:genesis + flock/msvcrt + O(n)整链校验+LedgerCorruptionError+64MiB轮转+export_hash` | 3.5/5 审计级 |
| dsh | `session-telemetry capture 141` vs `session-telemetry-otel export LoggerProvider→BatchLogRecordProcessor→OTLPLogExporter` 分离、`cordis.patch.yml: mode DSH_TELEMETRY_MODE||DISABLED shutdownTimeout3000 scheduledDelay10000 maxQueue2048`、**chunk 投影仅首个 assistant/chunk 采样** 186、冗余 `(session.id,seq)` 去重、瀑布脱敏 `record→next` 可叠加、`FULL` 发 `telemetry.op=shutdown` 判 crash | 5/5 教科书 |

**指位：** hero 需把 `otel.py:73 stub` 换 `opentelemetry-exporter-otlp BatchLogRecordProcessor` 接 `otel-collector:4318 /v1/logs`、heartbeat 4 层接真实探针、metrics 补 `http_request_duration_seconds histogram + circuit_state gauge`。

---

## 4. 安全 — 从无到 `flock + ZWSP + CSP + Landlock`

| 向量 | TradingAgents | hero-quant | vibe-trading | dsh |
|---|---|---|---|---|
| ticker 路径穿越 | `dataflows/utils.py:9 safe_ticker_component ^[A-Za-z0-9._\-\^=+]+$ len≤32 dots-only 拒` + `trading_graph 518 / checkpointer 22 / stockstats_utils 159` 多点调用 3/5 | 缺（无同类函数）→ 需照搬 TA | 同 TA | spill 不透明 locator 5/5 |
| checkpoint 隔离 | `checkpointer.py:42 per-ticker .db + thread_id sha256(ticker:date:signature)[:16] + signature 感知图形状` 3/5 | `checkpoint/postgres.py + temporal` 侧车已声明，未覆全 | ledger 即 checkpoint | Session log 即状态，无独立 checkpoint |
| AST/平台沙箱 | 无 0/5 | `sandbox/ast_guard.py:4-98 allowlist{pandas,numpy,scipy,math,typing} ban{socket,subprocess,ctypes,requests,os} + eval/exec/__import__` 2/5（过严、未接 runner） | `bwrap/landlock/seatbelt confine(argv)` + `tool-ask-user` 4.5/5 | `native/landlock-run C11 prctl(NO_NEW_PRIVS)+landlock_create_ruleset ABI probe exit125 --probe` 5/5 |
| 注入/脱敏 | 无 0/5 | `security/redaction.py:16 ARGUMENTS_SINK strict vs RESULT_SINK content 透传 + Bearer/sk-/AKIA/JWT` 2/5（未自动织入） | `security/scanner.py:26 5规则 + _SPECIAL_TOKEN_RE ChatML/Qwen/DeepSeek/Llama/Gemma + ZWSP 幂等 + with_security_warnings dotted *` 5/5 | `session-telemetry record waterfall` 可叠加 5/5 |
| 认证/DNS/CSP/SSE | 无（CLI only）0/5 | `api/server.py:30 Counter` 单点 1/5 | `api/security.py:1-669 HMAC compare_digest + _reject_untrusted_loopback_host + CSP frame-ancestors none + _SSE_TICKET_TTL 60s 单次 + AccessLogRedactionFilter + CORS * 拒` 5/5 | loop 隐含 |
| 账本治理 | 无 | `governance/ledger 55-96 per-tenant seq/prev_hash/record_hash sha256 + 0600` 2.5/5（无 flock/GENESIS/轮转） | 同前 738 行版 5/5 | session log 4.5/5 |
| Docker 加固 | `Dockerfile 2 段 python3.12-slim USER appuser` 无 cap_drop/read_only 1.5/5 | `Dockerfile 89 USER vibe + vibe-sandbox uid10001 + cap_drop ALL + read_only + tmpfs` 4/5 | `cap_drop ALL + read_only + tmpfs + pids_limit512 + extra_hosts` 5/5 | 非容器主线 |
| 供应链 | `pyproject 0.3.1` 无锁 1/5 | `requirements-lock.txt:1 uv --generate-hashes 695行 sha256 + Dockerfile --require-hashes` 4.5/5 | 双锁 `requirements-lock + requirements-channels-lock` + `allowBuilds` 5/5 | `pnpm 11.7 allowBuilds + patches` 5/5 |

**指位：** hero 补 `safe_ticker_component` 拷贝、`security/scanner + with_security_warnings` 自动织入 `TraceWriter/ledger/web_search`、`api/security 120 行 HMAC/DNS/CSP/Ticket` 移植、`sandbox/runner confine` 接 `vibe-sandbox` user、`ledger flock+GENESIS+轮转`。

---

## 5. 工程化 — 从无锁到 60 门 `run-gates`

| | TradingAgents | hero-quant | vibe-trading | dsh |
|---|---|---|---|---|
| 锁哈希 | 无 1/5 | `uv compile --generate-hashes` + `--require-hashes` 4.5/5 | 双锁 5/5 | `pnpm-lock.yaml` 5/5 |
| Dockerfile | 2 段 copy venv 无 HEALTHCHECK 无 digest 锁 | 3 段 `node:22-slim@sha256:6c7479… + python:3.11-slim@sha256:e031… + runtime HEALTHCHECK python urllib /live` 4/5 | 同 hero + 双锁 5/5 | `landlock-run prebuildify musl` |
| Compose | 单服务无 sidecar | 4 服务 `postgres:16→temporal:1.26→otel-collector:0.128→hero-quant 2g/1.5c + healthcheck` | 单主 + 前端 4g/2c/512 pids + extra_hosts | 无 compose |
| CI | 无 workflow | 无 workflow（glob .github/** 空）2.5/5 | ruff+knip | 15 workflows + `run-gates.ts 33KB 60+门 14 modes needs DAG + validateGateGraph DFS` 5/5 |

**指位：** hero 补 `run-gates` 等价或至少 `hash-lock dry-run + pip-audit + gitleaks + ruff + pytest --cov + knip` 的最小 CI；`bump.ts + NOTICES` 发布链。

---

## 6. 记忆系统

| 库 | 存储 | 索引 | 去重/隔离 | 轮转/GC | 证据 |
|---|---|---|---|---|---|
| TradingAgents | 单 `trading_memory.md` 以 `<!-- ENTRY_END --> 13` 分隔的 append-only | 无（线性 split） | `store_decision 42-44 line.startswith pending` 幂等；全局单文件无隔离 | `_apply_rotation 220 pending 永保 + resolved 超 max_entries 丢最旧 + tmp+replace 原子 160`；`get_past_context 70 n_same=5 n_cross=3` 仅 resolved 参与；同 ticker 仅当次 `trading_graph 306` | 4/5 简单可靠 |
| hero-quant | 双写 `*.md per key + memory.db SQLite 48-69` | `FTS5 trigram 58` 退化 plain + `notes` 表 | `last_write[key]==content && <30s 75-78`（重启丢）+ `namespace tenant:thread 15-46 __ 隔离 + 0600+fsync+replace 113` | 无 GC/轮转 | 2/5 MVP |
| vibe-trading | `*.md per entry + memory_index.db FTS5 23 + MEMORY.md 快照 211` | FTS5 CJK bigram `search_index 358-386` + 触发器自同步 176 | `content_hash(name+desc+content lower)[:12] 35-39 滑动 30s 457` + `memory_lock fcntl 5s 重试 42-73` + `hierarchy category 20` | `HALF_LIFE 14d 75 + lifecycle GC archive<0.15 delete<0.05 MIN_AGE 7 MAX 500 + compression raw→daily 7d TF-IDF→digest 30d 30-35` + `MAX_INDEX_LINES 200` 快照冻结 | 5/5 完整生命周期 |
| dsh | `SessionEvent[] 426` 内存，`session/flush` 插件持久 | `SurfaceManager.nodes[] + replaceGeneration` | `deepFreeze + seq=log.length 564 + reentrancy guard 624 + seq==expected 329` + `Carrier scopeTarget 915` 域隔离 | `compaction replace 影子 9-22 + toolResultPruner 按 Unicode 码点裁` | 5/5 日志语义（非记忆） |

---

## 7. RAG — 从字符串拼接走到 `BM25 K1=1.5 B=0.75 thr 0.3 + Ebbinghaus`

| 库 | 召回 | 排序 | TopK | 时序衰减 | 语义扩展 |
|---|---|---|---|---|---|
| TradingAgents | `get_past_context 70` 线性逆序走，同 ticker 全文 + 跨 ticker 仅 reflection/截断 300 注入 **仅 PortfolioManager 36-41** | 纯 recency + 同/跨桶 | 5+3 固定 | 无 | 无 |
| hero-quant | 3 段回退 `FTS5 MATCH 165 → LIKE 196 → file glob 210` 未用 rank | 无 rank，去重后全量返回 | 无界 | 无 | `mcp/router TopK5 15-61` 关键字 +3/+1/+0.5 伪向量；`embed.py 16-58` SHA256 伪 16 维 |
| vibe-trading | `persistent 362 FTS5 search 373 → token 扫描 412 METADATA_WEIGHT 2.0 → link 扩展 435` | `token_score*(0.5+0.5*importance) + FTS -rank*(0.5+0.5*importance)` `persistent 75-91 compute_importance=qs*(exp(-λ*days)+min(0.3,ac*0.1))`；`semantic_links BM25 K1=1.5 B=0.75 27 idf log((N-n+0.5)/(n+0.5)+1) 99 + thr 0.3 33` | FTS 5 + BM25 top5 cap10 184 + `effective_k min(top_k,10)` | Ebbinghaus 14d 加权于 FTS 与 token 双路 | Wikilinks `[[id]] 322` + relations.json 侧车原子落盘 232 |
| dsh | `deriveMessages 726 incremental fold + deriveEventMessage 83` 投影 | 位置序 | 全量 | 无 | surface `append vs {op:replace,start,end}` 16 压缩影子 + header 去重 `!headerEquals 465` |

**结论：** vibe 的 `FTS5 CJK bigram + BM25 + Ebbinghaus + 3 段回退 + link 扩展` 是四库唯一生产级 RAG；TradingAgents/hero 的检索均不及格；dsh 本就不是 RAG。

---

## 8. 七维雷达与 hero-quant 修复指位（P0→P2）

```
          记忆
         5 ·  ·
       ·    vibe 5 · hero 2
     ·  ·        ·   · TA 4
   RAG  ·  · · · · · ·  · Loop
    5 ·              ·1.5 TA
     ·  hero2  dsh5 ·  · hero2.5
      ·  · · · · · · ·
       工程  可观测  安全
       TA1 hero2.5 vibe3.5
       hero4.5 vibe5 dsh5
```

| P | 修复 | 证据指位 | 收益 |
|---|---|---|---|
| **P0** | AgentLoop 补分层上下文 `microcompact/collapse 0.5/0.7` + `len(buffer)//4` 估 token + `wrap_up_at 0.8*max + tools=None` 收尾 + `BudgetBreaker` 接真实 `usage_metadata` | `vibe loop 307/322/893/948 + hero loop 190/223/708/734` | token 熔断从“字符幻觉”变真实 |
| **P0** | Grounding 加 20+ 掩码 + `batch-frozen identity` + `get_verified_market_snapshot` 证据块 | `vibe grounding 241/362/397 + TA market_analyst` | 价位幻觉单拣从 12 行校验变机械门 |
| **P0** | OTel 接 `BatchLogRecordProcessor→4318`、`heartbeat 4 层真探针`、`metrics histogram+gauge` | `hero otel 73 + heartbeat 29 + dsh capture 141` | 观测从 stub 变可告警 |
| **P0** | 安全织入 `scanner+ZWSP + HMAC/DNS/CSP/Ticket + safe_ticker_component` | `vibe scanner 26 + security 1 + TA utils 9` | 5 向量从 2/5 → 5/5 |
| **P1** | Multi-Agent `Send fanout` + `轻 pros/cons` 抗辩（不做 3 轮风险链） | `TA setup 98-152 + vibe swarm 273 + hero graph 94` | 4 analyst wall-time ↓60% |
| **P1** | 记忆/RAG `pending→resolved + Ebbinghaus 14d + BM25 K1/B + hierarchy + GC 0.15/0.05` | `TA memory 13-255 + vibe persistent 75/lifecycle 93/semantic 27` | 检索从“线性 recency”变时间感知 |
| **P1** | 账本 `flock + GENESIS + 整链 O(n) + 64MiB 轮转 + RLS` | `vibe ledger 50-738` | 治理从演示变合规 |
| **P2** | LLM VCR `llm_usage per chunk + SessionEvent 全录` + 批量 Bench `run_batch(tickers,dates)→metrics` | `dsh Session 604 + hero trace 322` | 回测可离线 golden-run |

---

## 9. 证据索引（便于抽查行号）

- **TradingAgents 骨干：** `graph/setup.py:32,37,95,115,124,137` `graph/trading_graph.py:306,348,378,419,484` `graph/propagation.py:18,78` `graph/conditional_logic.py:52,63` `agents/utils/agent_utils.py:190` `agents/utils/memory.py:13,30,70,164,220` `graph/reflection.py:14,31` `dataflows/utils.py:9` `llm_clients/factory.py:182`
- **hero-quant 现状：** `agent/loop.py:36,190,233,257,313,564,636,697,734,767,858` `agent/policies.py:29,97` `agent/grounding.py:37,45` `agent/context.py:15,30,127` `agent/trace.py:28,81,322,413` `agent/embed.py:16` `memory/store.py:48,71,155` `mcp/router.py:15,64` `skills/loader.py:125` `telemetry/otel:16,73 heartbeat:10 circuit:17 api/server:13 governance/ledger:55 sandbox/ast_guard:4 security/redaction:16`
- **vibe-trading 参照：** `agent/loop.py:307,322,893,948,1364,1550 swarm/runtime:68,273 swarm/task_store:150 memory/persistent:75,211,362 memory/search_index:358 memory/semantic_links:27,179 hierarchy:70 lifecycle:79,183 compression:30 server/security:1 governance/ledger:50`
- **dsh 参照：** `packages/core/agent 256,341 agent-loop 296 agent.ts 245,332,407 session 426,552,604,726,1081 docs/architecture 64 compaction 9 interview-notes/dsh-engineering 14`

*本附件与主文档共同构成 Phase 1 交付；下一步请评审 P0 4 项优先级，确认后进入 `writing-plans → docs/plans/2026-08-21-hero-quant-tradingagents-tasks.md` 的 2-5 分钟 TDD 任务拆解与 Subagent-Driven Build。*
