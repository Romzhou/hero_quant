# Scan Remain 全量修复实施计划 — 916 条 / 187 文件

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 2周内分4批将 `scan_remain.log` 的 916 条（13 critical / 298 high / 448 medium / 157 low）风险清零 — critical/high 零遗留重跑可验，medium/low 允许 ≤10% 白名单，单测不跌。

**Architecture:** 分级×分域并行 worktree 隔离。P0(13) → P1(285 high) → P2(448 medium) → P3(low+债)，每批 5 域并行（S-sec/S-data/S-llm/D-frontend/E-link），每任务 TDD 2–5分钟，批后重跑扫描抽样 + 域回归，@oracle 总复审。

**Tech Stack:** Python 3.11, FastAPI, pytest, Rust quantlib, React+TS (Vite), open-code-review 1.11.0, structlog, worktree

**基线:** `197c0b1` 已合入 P0 S1 的 4 文件安全修复（approval/credentials/redaction/tools/redaction），下述任务在该基线之上。

---

## Batch P0 — Critical 剩余 9 条（Day 1–3）

### Task 1: quantlib/rust NaN 保留

**Files:**
- Modify: `src/hero_quant/quantlib/rust.py`
- Create: `tests/test_rust_nan.py`
- Test: `tests/test_rust_nan.py`

**Step 1: Write the failing test**

```python
import math, pytest
from hero_quant.quantlib.rust import sma  # 假设封装为 sma(data, window) 或对应调用
def test_sma_nan_preserved():
    data = [1.0, float('nan'), 3.0, 4.0]
    res = sma(data, 2)
    # NaN 不应被 0.0 污染，窗口含 NaN 的位置应为 NaN
    assert math.isnan(res[1]) and math.isnan(res[2])
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_rust_nan.py::test_sma_nan_preserved -v`
Expected: FAIL — `assert 0.0 nan` 或 SMA 返回 `[0.5,0.0,...]` 当前把 NaN 转 0.0

**Step 3: Write minimal implementation**

```python
# src/hero_quant/quantlib/rust.py 内所有 `[float(x) if x==x else 0.0 ...]` 替换为
import math, pandas as pd
def _to_rust_vec(data):
    return [None if pd.isna(x) else float(x) for x in data]
# 调用 _RUST_MOD.sma(_to_rust_vec(data), window) Rust 侧接受 Vec<Option<f64>> 并传播 NaN
```

对 SMA/EMA/RSI/Bollinger/MACD 5 处统一改为 `_to_rust_vec`，删除 `x==x` 脆弱检测。

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_rust_nan.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/quantlib/rust.py tests/test_rust_nan.py && git commit -m "fix(quantlib): preserve NaN via Option<f64>, no 0.0 coercion"`

---

### Task 2: checkpoint hash 确定性

**Files:**
- Modify: `src/hero_quant/checkpoint/postgres.py`
- Test: `tests/test_checkpoint_pg.py`

**Step 1: Write the failing test**

```python
def test_thread_to_keys_deterministic():
    from hero_quant.checkpoint.postgres import _thread_to_keys
    import os
    a = _thread_to_keys("wf:runABC:tenant1")
    # PYTHONHASHSEED 随机时两次结果应一致
    b = _thread_to_keys("wf:runABC:tenant1")
    assert a == b
    # seq 应可逆，不丢原始 run
    from hero_quant.checkpoint.postgres import list_thread_ids  # 验证往返
```

Command: `pytest tests/test_checkpoint_pg.py::test_thread_to_keys_deterministic -v` Expected FAIL (当前 `hash()` 随机)

**Step 3: Implementation**

```python
import hashlib
def _thread_to_keys(thread_id: str):
    wf, run, tenant = _validate_thread_id(thread_id)
    try:
        seq = int(run)
    except Exception:
        seq = int(hashlib.sha256(run.encode()).hexdigest()[:8], 16) % 2147483647
        # 同时持久化映射 run_str→seq 到 _TS，避免重构丢失；list_thread_ids 改查映射
    return tenant, f"{wf}:{run}", seq
```

修复 `list_thread_ids` 往返逻辑，移除 `abs(hash())%...` 碰撞。

**Step 4:** `pytest tests/test_checkpoint_pg.py -k thread -v` PASS

**Step 5:** commit

---

### Task 3: yahoo 宽 except 窄化

**Files:**
- Modify: `src/hero_quant/data/loaders/yahoo.py`
- Test: `tests/test_loader_yahoo.py`

Test: mock `yf.download` 抛 `TimeoutError`，断言不被转 `ImportError`，保留原异常类型或 `DataLoadError`。

Impl: 将 `except Exception: raise ImportError` 窄化为仅包裹 `import yfinance`，数据分支 `except (ConnectionError, TimeoutError, ValueError)` 分别处理。

---

### Task 4: registry synthetic 溯源统一

**Files:**
- Modify: `src/hero_quant/data/registry.py`
- Test: `tests/test_data_registry.py`

Test: 同一 loader 在 3 处分支应一致判定 `synthetic`，Settings 失败时不默认 live。

Impl: 抽 `def _is_synthetic(loader, result, prov) -> bool` 单一函数，缓存 `Settings().data_mode` 一次，统一 `class+source+name` 判断，删除重复 `Settings()` 实例化。

---

### Task 5: llm stream 重试仅首包前

**Files:**
- Modify: `src/hero_quant/llm/client.py`
- Test: `tests/test_llm_client.py`

Test: 模拟生成器已 yield 2 chunk 后抛 `ConnectionError`，断言不重试且已产出不重复。

Impl: 重试循环仅在 `first_yield==False` 时允许；首包后异常直接抛；`try/finally: gen.close()` 防泄漏。

### Task 6: shadow 熔断 fail-closed

**Files:**
- Modify: `src/hero_quant/shadow/service.py`
- Test: `tests/test_shadow.py`

Test: mock `circuit.allow` 抛异常，断言 `check_order` 拒绝而非放行。

Impl: `except Exception: return Reject(circuit_open)`，删除 `state=="OPEN"` 字符串双重检查。

### Task 7: correlation 合成数据标 provenance

**Files:**
- Modify: `src/hero_quant/tools/correlation.py`
- Test: `tests/test_correlation.py`

Test: 注入失败时断言 `ok==False` 且不返回伪相关 1.0。

Impl: `_fetch_closes` 失败抛 `ProvenanceError`，`compute_correlation` 接 `provenance=="synthetic"` 时直接 `ok=False`，删除 `[100+i*0.5]*40` 伪造。

### Task 8: otel sys.modules 隔离修复

**Files:**
- Modify: `tests/test_otel_maturity3.py`
- Test: `tests/test_otel_maturity3.py`

Impl: `sys.modules.clear()+update` 改 `monkeypatch.dict(sys.modules, ...)` 或 `unittest.mock.patch.dict`，删全局污染。

### Task 9: Risk 静默吞错显性化（@designer）

**Files:**
- Modify: `frontend/src/pages/Risk.tsx`
- Test: `frontend/src/__tests__/Risk.test.tsx`

Test: mock fetch 失败，断言出现 `警示横幅` 且不显示 `风控正常·CLOSED`。

Impl: 加 `error` state，失败设 `setError`，badge 改 `summary==null && !loading ? "数据异常" : CLOSED`，常量抽 `API.RISK_SUMMARY`。

---

## Batch P1 — High 285 条（Day 4–8，5 lane 并行）

### Task 10: S-sec 剩余 — SQL 参数化 + RLS 一致性

**Files:**
- Modify: `src/hero_quant/checkpoint/postgres.py:267`（expires_at 拼接）、`src/hero_quant/billing/service.py:258`
- Test: `tests/test_checkpoint.py::test_sql_injection_safe`

Impl: `expires_at` 改 `psycopg2.sql.Identifier/Parameterized` 或 `interval %s` 参数化；`list_factors` RLS 过滤与 `get_factor` 一致化。

### Task 11: S-data — akshare/ccxt 硬编码日期回退

**Files:**
- Modify: `src/hero_quant/data/loaders/akshare_loader.py:27,166`、`ccxt_loader.py:106,138`、`tencent.py:125`
- Test: `tests/test_loader_akshare.py`

Test: 日期解析抛异常时断言不回退到硬编码 `2020-01-01`，而是显式 `DataValidationError`。

Impl: 删除 `except: fallback_date`，改为 `raise` 并在 registry 层显性标记 synthetic。

### Task 12: S-data — falsy 零值与 HTTP 注入

**Files:**
- Modify: `src/hero_quant/data/loaders/tencent.py:100,125`、`scope.py:45`
- Test: `tests/test_tencent.py`

Impl: `float(x) if x else default` → `float(x) if x is not None else default`；URL 构造 `quote(symbol, safe="")` + 强制 https。

### Task 13: C-engine — backtest 对齐/杠杆/PIT

**Files:**
- Modify: `src/hero_quant/backtest/engine.py:155,402,448`、`metrics.py:40,109`、`validation.py:57,72`
- Test: `tests/test_backtest_engine.py`

Impl: `_align` 返回多资产 dict 而非单 float；PIT 默认开；杠杆用 `math.isclose`；`cummax==0` 时跳过除法；`costs` 参与 equity 扣除。

### Task 14: D-frontend — SSE 清理与重连风暴（@designer）

**Files:**
- Modify: `frontend/src/pages/Chat.tsx:17,68`、`Live.tsx:59,121`、`Monitor.tsx:121`
- Test: `frontend/src/__tests__/Chat.test.tsx`

Steps:
- Chat: `useEffect(()=>[abort,es close,rAF cancel])`，`parseSseData` 统一，`crypto.randomUUID()` 替换 `Date.now()`
- Live: `pausedRef` 修复闭包，`offsetRef` 移出 deps，`AbortController` 按迭代 abort
- Monitor: deps 去 `offset/cost/breakerState`，保留 `paused`，`reader.cancel()` 清理

### Task 15: D-frontend — CSV/iframe/硬编码（@designer）

**Files:**
- Modify: `frontend/src/pages/Research.tsx:29,372`、`Dashboard.tsx:24,36`

Impl: CSV 用带引号解析（或 Papercut 轻量），`slice(4000)` 按行边界截断；`srcDoc` 前 `DOMPurify.sanitize` + `sandbox=""`；抽 `config/api.ts` 常量。

### Task 16: E-link — stream/telemetry/mcp

**Files:**
- Modify: `src/hero_quant/stream/service.py`、`telemetry/heartbeat.py`、`mcp/server.py:63`

Impl: stream `IncrementalFactor` 去重，telemetry `sidecar_heartbeat_probe` 真正心跳，mcp 输出 schema 校验。

---

## Batch P2 — Medium 448 条（Day 9–12）

### Task 17–22: Medium bug/security 170 条按域分 6 任务
- 17: `core/scope:55` 浅拷贝修 `copy.deepcopy`
- 18: `quantlib/indicators:39` 窄化 except + `_validate_window` 容差
- 19: `backtest/metrics:61` CAGR `n=len(s)-1` 年化修正
- 20: `checkpoint/postgres:233` `asetup` 重试 guard 翻转修复
- 21: `billing/service:105` `publish_factor` 已存在校验不静默覆盖
- 22: 其余 medium 按 `grep '[medium]' scan_remain.log | fzf` 批量处理，每文件 `ocr-ignore` 白名单理由注释

每个子任务独立 TDD：先 `pytest -k <domain> -v` 复现 → 修 → 绿 → `git commit`。

---

## Batch P3 — Low + Maintainability + Test 债务（Day 13–14）

### Task 23: maintainability 194 — 硬编码与重复抽取

**Files:**
- Modify: `frontend/src/pages/*.tsx`、`src/hero_quant/config/limits.py:43`

Impl: `METRICS_URL/ROUTES/DEMO_QUERY` 等抽 `frontend/src/config/api.ts`，`limits.truncate` 修复 `s[:lim]+...` 超长；重复 `fetch` 抽 `useFetchArtifact`。

### Task 24: style/performance 38 — 嵌套三元与 GPU 优化

**Files:**
- Modify: `frontend/src/index.css:27,14`、`Live.tsx:24`、`Research.tsx:369`

Impl: 嵌套三元抽 `getBreakerState`/`getToolStatusClasses`，grain overlay 加 `will-change: transform` 限 `pointer-events:none`，inline style 改 `h-[300px]`。

### Task 25: test 248 — 白名单与隔离

**Files:**
- Modify: `tests/*.py` 被标 test 类型的误报加 `# ocr-ignore: <reason>` 或规则白名单 `.ocr.yaml`，`test_vcr`/`test_vector_maturity4` 等加 `monkeypatch` 隔离。

每任务后 `git add -f docs/plans/*` 若涉及。

---

## Batch 验收与 Review（贯穿）

### Task 26: 每批重跑扫描抽样 + 域回归

Command: `npx @alibaba-group/open-code-review@1.11.0 --include "src/hero_quant/<domain>/**" --format text 2>&1 | tee scan_batch< N>.log`
Expected: 对应批次 critical/high 0 遗留

Command: `pytest tests/test_<domain>*.py -q --maxfail=1`
Expected: PASS，覆盖率不跌

### Task 27: @oracle 总复审 + 合并

Files: 全量 diff

Step: `task(oracle)` 审熔断/审批/回测对齐/脱敏，`designer` 审前端视觉无损，`fixer` 仅机械收尾保留设计。

Step: `git branch --show-current` → `gh pr create` 或 `git merge --no-ff`，`worktree remove`

---

## 执行方式

Plan saved to `docs/plans/2026-08-28-scan-remain.md`. Two execution options:

1. **Subagent-Driven** — I dispatch a fresh sub-agent per task, review between tasks (recommended for 2周并行, worktree隔离, TDD)
2. **Manual** — You run the tasks yourself

Which approach?
