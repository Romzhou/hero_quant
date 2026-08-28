# Scan Core Muse Fix 设计 · 2026-08-28

> **For implementer:** 严格 YAGNI 最小改。TDD 全程：先失败用例 → 最小补丁 → 变绿 → 提交。禁止顺手重构无关代码。
> **源:** `scan_core_muse.log` (31 文件, 226 评论, ~1.8M tokens, 22m30s, Session 47ff3709)  §Project Summary Top8
> **基线:** `master@db69748` (Wave6 闭环, 302 passed, cov 48.99%)

**Goal:** 全量修复 `scan_core_muse.log` 226 条评论，Top8 为验收单元，复扫高/关键零残留，`pytest` 全绿且 `coverage≥50` + `ruff` 零新增告警。

**Architecture:** 6 波按热点串行、每波独立分支与可演示验收，共享 3 个横切契约（`threading.Lock` 并发、`canonical_json` 哈希统一、`Path.resolve().is_relative_to()` 路径校验），严格 YAGNI 不换 DB/框架选型，fail-closed 替代静默回退。

**Tech Stack:** Python 3.11, FastAPI/Starlette, LangGraph, SQLite FTS5 + pgvector, `threading.RLock`, `hashlib.sha256`, `pathlib`, `prometheus_client`, `structlog`, `pytest`/`pytest-cov`/`ruff`

---

## 1. 架构总览

```
Wave1 Sandbox (Top1)           ┐
Wave2 Governance Dedup/Ledger (Top3) ├── 串行执行，单写属主，无并发写属主重叠
Wave3 API Auth/Risk/Server (Top2+Top4) │   每波：Plan → TDD子代理构建 → 双审 → 合并
Wave4 Agent Loop/Graph/Policies (Top7) │
Wave5 Memory Store/Hierarchy (Top6)    │
Wave6 Embed/Grounding/Context/Prompt/Trace/WallTime (Top5+Top8) + 全量复扫 ┘
```

* **节奏:** 6 波 × 0.5–1d，单波 2–5 分钟/任务粒度，波间 `git merge` 到 `master`，可回滚。
* **横切契约 (3):**
  1. 并发：`BudgetBreaker`/`Dedup._mem`/`MemoryStore._recent_hashes`/`_backtest_cache` 统一 `threading.Lock` + `check_and_add` 原子化。
  2. 哈希：`ledger.compute_record_hash` 唯一正典（`sort_keys, separators=(',',':'), ensure_ascii=True, sha256:` 前缀），`append/_verify_entries/build_export` 全调它，废 `str()` 路径。
  3. 路径：所有 `trace_dir/replay_path/_dist_path/hierarchy route_entry/workspaceRoot` 统一 `Path(x).resolve()` + `is_relative_to(base.resolve())` + 拒绝 `..`/`/`/`:`/`\n` 与绝对路径穿越。
* **YAGNI 边界:** 不新增功能、不换持久化选型、不做分库分表；仅最小补丁闭环评论；`Sandbox` 不引入新依赖，仅窄化放行。

## 2. 组件与职责

| Wave | Top 映射 | 文件 | 职责 | 验收 |
|------|----------|------|------|------|
| **1 Sandbox** | Top1 沙箱绕过 | `sandbox/ast_guard.py` `base.py` `policy.py` `runner.py` `__init__.py` | 补 `import as` 别名映射、`open/compile/getattr` 禁调、`_get_root_name` 链式解析、`shell=True` 拒 `str`、`canonical_path` 严格回退、`derive_key` 转义、探针 `Lock`、`dispatch_tool` 强制 `require_enforcement` | `test_sandbox_bypass.py` 别名/链/builtins 全拦截；`str cmd` 抛 `ValueError`；`dispatch_tool` workspace-write 不可 unconfined |
| **2 Governance** | Top3 幂等/账本 | `governance/dedup.py` `ledger.py` `wall_time.py` `reconcile.py` `__init__.py` | 补 `tenant` 列+执行 `DDL_TOOL_CALL_PG`、`CREATE POLICY IF NOT EXISTS`、去 `= '' OR NULL` 绕过、`:` 转义/hash、`BEGIN IMMEDIATE` 原子 `insert_pending`、`tool_call_dedup` 创建、`cur.rowcount` 替代 `total_changes`、`ledger` 统一哈希+加锁 `fsync(dir)`+修复 `time_call` finally 吞异常 | `test_dedup_rls_tenant.py` 租户隔离；`test_ledger_hash_consistency.py` 哈希一致；`test_wall_time_finally.py` 不吞原异常 |
| **3 API** | Top2/Top4 鉴权+风控 | `api/security.py` `api/server.py` `api/risk.py` | 修 HMAC 真校验（`await request.body()`）、`verify_api_key` 空 key 拒、`host` 空即 403、`candidate.resolve().is_relative_to(dist)`、`trace_dir/replay_path` 白名单、`mock 0.42/0.62` 接真实源或 `degraded`、`CircuitBreaker` 单例、`REQUEST_COUNTER` 空 guard、`mkdtemp` 改 `TemporaryDirectory`+流式读 | `test_security_hmac_real.py` 假前缀 401；风险端点 `degraded` 标记 |
| **4 Agent** | Top7 循环/预算 | `agent/loop.py` `graph.py` `policies.py` `state.py` | 修线程泄露（不用 `cancel()` 幻觉）、`BaseException→Exception` 放行中断、`replay_path` 约束、`_breaker` 加锁+`check_and_add`、`delegation_depth+1`、`BudgetBreaker NaN/Inf` 校验、`state` 显式 reducer、`RetryPolicy asyncio.sleep` 双路径 | `test_loop_keyboard_interrupt.py` Ctrl-C 直透；`test_breaker_nan.py` Na 不毒化 |
| **5 Memory** | Top6 存储 | `memory/store.py` `hierarchy.py` `lifecycle.py` `rank_fusion.py` `rerank.py` `ingest.py` | 加 `RLock` 护 `dict`/`cache` + WAL + `close()`、`write` 原子化（失败删文件/启动 reconcile）、hash 扩至 16 hex+唯一 tmp、`route_entry` 校验穿越、`lifecycle` 备份原子+不丢数据、`rank_fusion` 去空 key、`ingest` 校验 `chunk/overlap`+`is_file` | `test_memory_thread_safety.py` 并发不丢；`test_hierarchy_traversal.py` `/etc/passwd` 拒 |
| **6 Embed/Context** | Top5/Top8 向量/上下文 | `agent/embed.py` `grounding.py` `context.py` `prompt.py` `trace.py` `governance/ledger`+`reconcile` 余量 | `_embed_offline` 归一+缓存模型、`from_pgvector` 抛错、`grounding` 归一统一+空证据闭合、`context` stale `total_chars` 重算+`extra_rules` 透传+转义、`trace` 全量修复（阈值校验、关闭标志、sidecar 全 digest） | `test_embed_normalized.py` 离线余弦可比；`test_grounding_empty.py` 空账本拒 0；`test_trace_durability.py` fsync dir |

*写属主隔离:* 波内仅改本波文件，横切契约变更通过 `docs/plans` 契约段同步，避免并发改同一行。

## 3. 数据流

```
用户 q / tool cmd
  → api/server _security_headers_and_host_check (host 白名单 fail-closed)
  → api/security verify_hmac/verify_api_key (真 HMAC compare_digest)
  → sandbox/ast_guard check_source (别名映射 + 链式 root + builtins) ─┐
  → sandbox/policy is_path_writable (resolve + is_relative_to, TOCTOU 声明) │
  → sandbox/runner confine/execute (str 拒, require_enforcement 真值)      ├─ 阻断即 tool_error/403
  → governance/dedup insert_pending (BEGIN IMMEDIATE, tenant 隔离)        │
  → governance/ledger append (加锁 → 全链增量校验 → fsync 文件+目录)      │
  → agent/loop run (replay_path 白名单, BaseException 放行, 超时不泄线程) │
  → agent/graph plan→Send fan-out (breaker check_and_add 原子, depth+1)   │
  → memory/store write/search (RLock + WAL, 双写原子, dedup 真 hash)      │
  → memory/hierarchy route_entry (校验后落盘, 原子 replace)               │
  → agent/context compact (重算 total_chars, 预算 clamp) + prompt 拼接(转义)│
  → agent/trace TraceWriter (阈值校验, sidecar 全hash, 流式读, fsync dir) ┘
  → governance/reconcile daily_reconciliation (单次预算 enforcement, 不双计)
```

*Fail-closed 原则:* 鉴权/沙箱/账本任一失败 → 直接 `403`/`SandboxUnavailableError`/`LedgerCorruptionError`，不静默 fallback 到 `memory`/`no-op`；仅 `pgvector/Cohere` 等可选增强可 degraded 并 `warning`+`metrics` 可观测。

## 4. 错误处理

* **窄化捕获:** `except (OSError, ValueError, TypeError, ImportError)` 替代 `except Exception: pass`；`redact_payload` 失败 → 丢弃并 `{"redaction_error":True}` 不落无脱敏数据；`ledger` chmod/fsync 失败 → `warning` 且可 `verify()` 检出，`append` 加锁失败则抛不继续无锁写。
* **可观测:** 每处 `except` 配 `logger.warning/exception(exc_info=True)` + `DEDUP_OP_TOTAL{status=error}` / `inc_wall_time_exceeded` 计数；`_warn_fsync_failure` 改限流而非永久静默。
* **超时与中断:** `loop`/`policies` `except BaseException` 改 `except (KeyboardInterrupt, SystemExit, GeneratorExit): raise` 其余 `Exception`；`wall_time.time_call` 计时移出 `finally` 再 `raise WallTimeExceeded`，`__exit__` 单次 `observe_wall_time`。
* **边界校验:** `trace`/`store`/`rerank`/`ingest` 阈值 `>0` 校验，非法则 `warning+default` 或 `ValueError`；`BudgetBreaker` `math.isfinite` 拒 `NaN/Inf`。

## 5. 测试策略

* **TDD 每条评论:** 任务模板 2–5 分钟 — `Step1 失败用例` → `Step2 watch fail` → `Step3 最小实现` → `Step4 watch pass` → `Step5 commit`。用例命名与评论行号对应（如 `test_ast_guard_alias_evasion_embed125`）。
* **验收单元 Top8:** 每波末跑 `pytest -q -k <wave>` + `ruff check` + 针对该 Top 的复扫断言；全量结束跑 `pytest -q --cov --cov-fail-under=50`（基线 302, 当前 48.99→50）+ `ruff check src` 零新增 + `full-scan` 复扫 `critical/high=0`。
* **证据:** 每波产 `scan_<wave>.log` 增量对比；最终 `scan_core_muse.log` 高/关键清零报告；`governance/ledger verify_chain` 与 `trace read` 流式 OOM 回归用例保留。
* **门禁:** `tests/test_docs_honesty.py` 式诚信断言复用；`SandboxUnavailableError` 身份统一断言；`HMAC` 真向量断言（`hmac.compare_digest`）。

---

## 6. 风险与 YAGNI

* **回滚:** 每波独立分支 `fix/scan-core-wave{1..6}`，`merge --no-ff`，可单波 `revert`。
* **不做:** 不换 `asyncpg` 驱动选型、不引 `Redis` 替代内存票据、不做分库分表、不扩 `Cohere` 多模型对比、不改前端复杂可视化。
* **残留:** `wall_time.clock` 注入仅线 `self._now`，不再扩；`api/metrics` 私有 `_names_to_collectors` 改公有 `get_sample_value` 但不重写注册表。

## 7. 交付物

* 设计本文档 `docs/plans/2026-08-28-scan-core-muse-fix-design.md`
* 6 份实现计划 `docs/plans/2026-08-28-scan-core-wave{1..6}.md`（下一步 Writing Plans 产出）
* 代码分支 `fix/scan-core-muse` + 6 波子分支
* 最终 `scan_core_muse_rescan.log` + `pytest --cov` 报告

> 下一步: 进入 **Phase 2 Writing Plans**，产出 6 波任务级计划（每任务含精确文件路径、完整失败用例代码、精确命令与期望输出）。
