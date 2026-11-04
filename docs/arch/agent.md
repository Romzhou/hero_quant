# Agent 模块深挖（链A问答内核）

> 依据：`docs/recon.md` §3（链A）、`docs/issues.md` 附链路行号。行号为 2026-10-19 快照实测值；凡与 recon 初值漂移处均标注“漂移”。
> 白名单：仅新建本文，不改任何源码。

## 1. 职责一句话

`src/hero_quant/agent/` 是问答链A的推理内核：驱动 LLM 流式输出与工具执行的往返循环直至终止（壁时间/轮次/token/接地性/预算五重终止条件），并把轨迹落盘 `trace.jsonl`、向 WS 广播。

## 2. 关键文件与行号

| 文件 | 行 | 内容 |
|---|---|---|
| `src/hero_quant/agent/loop.py` | `class AgentLoop:` | 状态机主体，docstring 列 11 个主循环控制点（与 recon 一致，未漂移） |
| `src/hero_quant/agent/loop.py` | `def __init__(...)` | 装配 `llm / max_iterations=5 / token_limit=60000 / trace / context_manager / grounding / use_graph / graph / budget_breaker / retry_policy / memory_store / skills_loader` |
| `src/hero_quant/agent/loop.py` | `def run(self, goal: str) -> LoopResult:` | 主循环入口（recon 称 `AgentLoop.run(loop.py)`，精确入口行实为 564；102 为类定义行） |
| `src/hero_quant/agent/trace.py` | `class TraceWriter:` | 轨迹落盘 `trace.jsonl` 写者 |
| `src/hero_quant/agent/grounding.py` | `class GroundingLedger:` | 接地性账本，`assert_price` 失败进入纠正轮，耗尽轮次 → `grounding_failed`（见 `loop.py` 控制点 8）；批次17 起 `ingest` 由 `AgentLoop._ingest_grounding_from_tool_result` 在工具结果收口调用，`use_graph=true` 的委托路径不 ingest |
| `src/hero_quant/agent/context.py` | `class ContextManager:` | 上下文压缩，阈值 `> token_limit*0.8` 调用 `compact`（控制点 9） |
| `src/hero_quant/agent/policies.py` | `class BudgetBreaker:` | 预算熔断，`should_fallback` → `budget_fallback`（控制点 10） |
| `src/hero_quant/agent/graph/` | 目录 | LangGraph 可选分支；委托失败 → `graph_error`（控制点 11）；`use_graph/graph` 由 `__init__` 注入 |
| `src/hero_quant/llm/factory.py` | `class LLMFactory:` | 推理装配入口 |
| `src/hero_quant/llm/factory.py` | `def model_for_stage` | 按阶段选模型 |
| `src/hero_quant/llm/factory.py` | `def create` | 创建 LLM（含无 key 时 `_FallbackChat:35` 兜底） |
| `src/hero_quant/llm/client.py` | `class LLMClient:` | 底层客户端 |
| `src/hero_quant/tools/registry.py` | `def get_definitions` | 工具定义装配，给 `bind_tools` 用 |

## 3. 上游（谁调我）

- `src/hero_quant/api/server.py` `app = FastAPI(...)`（recon 写 `:322`，实测 **漂移+146 行**，见 `docs/issues.md` 附节思路：以实测为准）。
- 问答入口：`POST /v1/query/ticket` 实测 `server.py`（recon 写 `:1235`，**漂移**）；SSE 流 `event_generator` 实测 `server.py`（recon 写 `:1338`，**漂移**）；流响应返回实测 `server.py` `StreamingResponse(event_generator(), ...)`。
- 装配顺序（链A）：签票 gating（`_check_rate_limit → consume_ticket → Settings()`，见 recon §3）→ `LLMFactory.create + model_for_stage` → `bind_tools(get_definitions)` → `TraceWriter + GroundingLedger + ContextManager + BudgetBreaker + _get_checkpoint_saver`（cached saver，见 `server.py` `_checkpoint_saver_cache` 注释“避免每次新建池+TCP”）→ `AgentLoop.run`。

## 4. 下游（我调谁/落哪）

- 落盘：`trace.jsonl`（经 `TraceWriter`，`agent/trace.py`）+ ledger 内存 + checkpoint saver + WS 广播（`src/hero_quant/api/ws.py` `run_trace_consumer`）。
- 数据工具：经 `tools/registry`（`registry.py`）→ `tools/market_data/`、`tools/backtest.py` `run_backtest`；记忆经 `MemoryStore`（见 `docs/arch/memory.md`）。
- 检查点：`src/hero_quant/checkpoint/postgres.py`（缺配 `raise` 见 `:284-286`；`MissingRunMapping:252` fail-closed）。

## 5. 降级语义（fail-closed / fail-open 明细）

| 降级点 | 语义 | 依据 |
|---|---|---|
| 无 LLM key → `_FakeLLM` / `_FallbackChat` | **必须显式** `degraded:true + reason`（body）+ `X-Degraded` 响应头；前端横幅。现状为 P1-1 缺陷，未打标 | `server.py` / `:1387` 两处 `class _FakeLLM`（`docs/issues.md` P1-1）；头工具 `server.py` `_degraded_reason_header_value/_with_degraded_headers`；`llm/factory.py` `_FallbackChat` |
| PG 不可达跳 DDL 走 `emulated` | **必须打标**并在 `/ready` + `/v1/*` 显式声明；现状 P1-2 探针仍报 ready | recon §4 P1-2；`server.py` `_is_emulated` 判据注释“非真实 PG 即 degraded” |
| LLM 调用失败 | `RetryPolicy.should_retry` + 指数退避，重试耗尽 → `llm_error` 终止（fail-visible，不伪造答案） | `loop.py` 控制点 5 |
| 接地性校验失败 | 纠正轮 → 耗尽 → `grounding_failed`（不输出未接地价格） | `loop.py` 控制点 8；`grounding.py` |
| 预算耗尽 | `BudgetBreaker.should_fallback` → `budget_fallback`（降级终止，非静默） | `policies.py`；`loop.py` 控制点 10 |
| graph 委托失败 | → `graph_error`，回落直跑语义由调用方决定 | `loop.py` 控制点 11 |

## 6. 已知问题挂钩（只引用，不修）

- P1-1 `_FakeLLM` 无感知（`server.py`）：归 D 线。
- P2-1 `server.py` 30+ 静默（含链A异常升级需求，见 `server.py` 注释“链A异常升级 warning/error”）：归 D 线。
- P2-4 `/ready` 双探误报与链A就绪语义：归 D 线（复用池已在 `server.py` 落地 `_READY_POOL` 全局池）。

## 7. 给 D/E 线的接口契约备忘

- D 线若改 `server.py` 链A段：保持 `event_generator`（`:1698`）的 SSE `type=tool` 事件与正文去重语义（`_clean_agent_text:238` 移除 `[tool xxx result]` 内联 JSON 行）。
- E 线消费：降级头 `X-Degraded/X-Degrade-Reason`（`server.py`）+ body `degraded:true + reason`（`server.py` 已有两处 `degraded=True` 先例）。
