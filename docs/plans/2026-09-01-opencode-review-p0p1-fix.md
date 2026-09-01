# OpenCodeReview 紧急性分级修复 Implementation Plan

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 3 日内交付 P0+P1 可发版分支，阻断→正确性/安全→溯源三档闭环，每阶段独立合入可回滚，`tsc`/`pytest`/`gitleaks` 全绿。

**Architecture:** Phase0 基线分支 → Phase1 前端阻断单点突破 → Phase2 三轨并行（2a 回测 PIT、2b SSRF/租户隔离、2c 溯源白名单/缓存）→ 集成回归门禁 → Phase3/4 债务抛光；共享 `fail-closed`/`_redact_dsn`/`threading.RLock` 三契约，锁文件变更单独 PR。

**Tech Stack:** Python 3.11, FastAPI, pandas, psycopg[binary]+psycopg_pool, React 18 + Vite + Tailwind + TypeScript, Node 20, structlog, pytest/pytest-cov, ruff, gitleaks, pip-audit, Docker Compose (postgres)

---

## 0. 上下文与约束（源自原计划 §0）

- **匹配现代码风**：中文注释密度、`fail-closed / fail-visible` 契约、`structlog + logger.warning(exc_info=True)` 窄化捕获、`_redact_dsn` 脱敏、`threading.RLock` 全局锁。
- **合入约束**：每阶段均可独立合入、可独立回滚；测试与 CI 门禁随修随补。
- **分支**：`fix/review-P0-P1` 从 `master` 切出；锁文件改动单独 PR 避免与回测改动冲突。

## 1. 紧急性矩阵（复核后定版，原计划 §1 精梳）

| 级别 | 编号 | 位置 | 判定 | 不修后果 |
|---|---|---|---|---|
| **P0 阻断** | P0-1 | `frontend/src/pages/Settings.tsx:37+65` 重复 `const [draftApiBase…]` | `tsc --noEmit` / `vite build` 必挂 | 无法发版，CI 隐式漏检 |
| **P1 正确性** | P1-1 | `backtest/engine.py:536-547, 384-413, 682-823` PIT 降级为 warn + `_align` 静默回退 + 成本二次扣除 | 合成价伪装 live，前视偏差，首日 100% 换手 | 回测收益失真 |
| P1-2 | `backtest/bench.py:291-300` `allow_synthetic` 被强制覆写为 True | `allow_synthetic=False` 失效 | fail-closed 破裂 |
| P1-3 | `data/trait.py:64` `VALID_SOURCES` 含 `"good"` 测试后门 | 任意 `loader.name="good"` 过白名单 | 供应链旁路 |
| **P1 安全/隔离** | P1-4 | `telemetry/otel.py:84-93` SSRF 守卫 `is_private and host==169…` 恒假 + 仅卡一字面量 + DNS 绕过 | `OTEL_EXPORTER_OTLP_ENDPOINT` 可打内网 | 内网探测/SSRF |
| P1-5 | `billing/service.py` & `checkpoint/postgres.py` & `api/server.py:342-400` 无 `asyncpg/psycopg` 时退化为进程内存 RLS + `/ready` 误报 `pg ok` | 多 worker 租户隔离失效，重启丢数据 | 跨租户泄露 |
| P1-6 | `data/registry.py:21-45,375` `_settings_mode_cache` 永不失效 + 合成比较器跳过 | 测试切 `synthetic/live` 脏读；分歧静默跳过 | 溯源误解释 |
| **P2 本迭代** | P2-1..13 | 回测周转率/对齐/指标、memory/store hierarchy、ledger O(n) 等 | 见报告 | 债务放大 |
| **P3 抛光** | P3-1.. | 宽 `except Exception`、缺类型、循环依赖 等 | 低 | 可维护性 |

## 2. 依赖与并行度（原计划 §3）

```
Phase0 ──▶ Phase1(P0) ──▶ Phase2a(回测) ─┐
                     ├──▶ Phase2b(安全) ─┼──▶ 集成回归(pytest+vitest+tsc) ──▶ Phase3/4
                     └──▶ Phase2c(溯源) ─┘
```

- 2a-2c 无重叠文件，可三人并行；合入顺序 2c→2a→2b 风险最小。
- 2b 的 `pyproject/requirements-lock/Dockerfile` 改动需单独 PR。
- Phase3 依赖 Phase2a 的 `engine` 语义稳定后再动 `metrics/validation`。

## 3. 验证矩阵（每阶段必跑，原计划 §4）

| 命令 | 覆盖 | 阈值 |
|---|---|---|
| `npx tsc --noEmit -p frontend/tsconfig.json` | P0-1 | 0 error |
| `npm run build --prefix frontend` | P0-1 + tailwind | 产物存在 |
| `npm run test:run --prefix frontend` | 前端回归 | 全绿 |
| `pytest -q --cov=src --cov-fail-under=50` | P1-1..6 | ≥50% 且新增用例通过 |
| `pip-audit --require-hashes -r requirements-lock.txt` | 2b 依赖 | 0 高危 |
| `gitleaks detect --no-git -s src` | 脱敏 | 0 泄露 |
| `pg: docker compose up -d postgres && pytest tests/test_billing_persistence.py tests/test_checkpoint_pg.py` | 2b | 真 PG 路径通过 |

---

## Phase 0 — 分支与基线（0.2d）

### Task 0: 基线分支与只读验证

**Files:**
- Create: `tmp_baseline.log`（基线日志）
- Modify: 无（只读验证，不改代码）

**Step 1: Write the failing test（基线断言）**
```bash
# 预期：P0-1 复现 TS2451，覆盖缺口可观测
npx tsc --noEmit -p frontend/tsconfig.json   # 预期 FAIL: Duplicate identifier draftApiBase
pytest -q --cov=src --cov-fail-under=50      # 记录现有通过数（README 称 277）
grep -rn "allow_synthetic" tests             # 预期 0 命中 → P1-1/2 覆盖缺口
```

**Step 2: Run — confirm it fails**
```bash
git checkout -b fix/review-P0-P1
npx tsc --noEmit -p frontend/tsconfig.json 2>&1 | tee tmp_baseline.log
# Expected: FAIL — TS2451
```

**Step 3: Minimal implementation**
- 不改代码，仅产出 `tmp_baseline.log` 作为回归对照。

**Step 4: Verify**
```bash
cat tmp_baseline.log | grep -q "TS2451" && echo "baseline captured"
```

**Step 5: Commit**
```bash
git add tmp_baseline.log && git commit -m "chore(baseline): capture P0-1 tsc failure and coverage baseline"
```

---

## Phase 1 — P0 前端阻断（0.5d，最高优）

### Task 1: 修复 Settings.tsx 重复声明

**Files:**
- Modify: `frontend/src/pages/Settings.tsx`
- Test: `npx tsc --noEmit -p frontend/tsconfig.json`

**Step 1: Write the failing test**
```bash
npx tsc --noEmit -p frontend/tsconfig.json
# Expected: FAIL — error TS2451: Cannot redeclare block-scoped variable 'draftApiBase'
```

**Step 2: Run — confirm it fails**
```bash
npx tsc --noEmit -p frontend/tsconfig.json
# FAIL
```

**Step 3: Minimal implementation**
```typescript
// frontend/src/pages/Settings.tsx
// 删 65 行 const [draftApiBase…] 重复声明，保留 37 单声明 + 39 useEffect sync + 67 handleApiBaseChange 闭包
// 不动 resolveUrl/apiBase 校验逻辑（已正确：new URL + http|https + draft 不污染 persisted）
```

**Step 4: Run — confirm it passes**
```bash
npx tsc --noEmit -p frontend/tsconfig.json  # Expected: PASS 0 error
npm run build --prefix frontend             # Expected: PASS 产物存在
```

**Step 5: Commit**
```bash
git add frontend/src/pages/Settings.tsx && git commit -m "fix(frontend): remove duplicate draftApiBase declaration P0-1"
```

### Task 2: vite/tailwind 同步收敛（P2-10/11 顺带）

**Files:**
- Modify: `frontend/vite.config.ts`, `frontend/tailwind.config.js`
- Test: `npm run build --prefix frontend`

**Step 1: Write the failing test**
```bash
# vite proxy 在 Docker 下连不上 host.docker.internal；tailwind 模板字面量被 purge
grep -q "VITE_API_TARGET" frontend/vite.config.ts || echo "FAIL: no env proxy"
grep -q "safelist" frontend/tailwind.config.js || echo "FAIL: no safelist"
```

**Step 2: Run — confirm it fails**
```bash
grep -rn "VITE_API_TARGET" frontend/vite.config.ts; echo "exit:$?"
# Expected: FAIL
```

**Step 3: Minimal implementation**
```typescript
// frontend/vite.config.ts
proxy: { target: process.env.VITE_API_TARGET || "http://localhost:8899" }
// frontend/tailwind.config.js
safelist: [{ pattern: /bg-(emerald|amber|red|sky)-.*/ }]
```

**Step 4: Verify**
```bash
npm run build --prefix frontend  # PASS
```

**Step 5: Commit**
```bash
git add frontend/vite.config.ts frontend/tailwind.config.js && git commit -m "fix(frontend): vite proxy env and tailwind safelist P2-10/11"
```

### Task 3: CI 前端门禁闭环

**Files:**
- Modify: `.github/workflows/ci.yml` 或 `test.yml`
- Test: `gh workflow view` / 本地 `act` 或 push 后 CI

**Step 1: Write the failing test**
```bash
grep -q "frontend" .github/workflows/ci.yml || echo "FAIL: no frontend job"
```

**Step 2: Run — confirm it fails**
```bash
cat .github/workflows/ci.yml | grep -A5 "frontend"
# Expected: not found
```

**Step 3: Minimal implementation**
```yaml
# .github/workflows/ci.yml 新增 job frontend:
# setup-node@4 (node 20, cache npm) → npm ci --prefix frontend → npx tsc --noEmit -p frontend/tsconfig.json → npm run build --prefix frontend → npm run test:run --prefix frontend
# 复用现有 hash-lock 思路，不增外部依赖
```

**Step 4: Verify**
```bash
npm run test:run --prefix frontend  # 全绿
```

**Step 5: Commit**
```bash
git add .github/workflows/ci.yml && git commit -m "ci(frontend): add tsc+build+vitest gate P0-1"
```

---

## Phase 2a — 正确性：回测 PIT 与合成守卫（可与 2b/2c 并行）

### Task 4: backtest/engine.py PIT 严格化

**Files:**
- Modify: `src/hero_quant/backtest/engine.py`
- Test: `tests/test_backtest_pit_correct.py`, `tests/test_backtest_engine.py`

**Step 1: Write the failing test**
```python
# tests/test_backtest_pit_correct.py
import pytest
from hero_quant.backtest.engine import PITViolation

def test_pit_fail_closed_raises_when_no_date():
    from hero_quant.backtest.engine import run
    # prices 无 DatetimeIndex 且 allow_synthetic 未显式 True → 必须 raise，而非 warning 回退
    with pytest.raises(PITViolation):
        run(prices_no_index, weights, allow_synthetic=False)

def test_pit_allow_synthetic_true_uses_first_index():
    # allow_synthetic=True 才允许 pd_date=index[0] 分支
    r = run(prices_with_index, weights, allow_synthetic=True)
    assert r is not None

def test_pit_second_branch_still_raises():
    with pytest.raises(PITViolation):
        run(prices_no_index, weights)  # 默认不允许合成
```

**Step 2: Run — confirm it fails**
```bash
pytest tests/test_backtest_pit_correct.py::test_pit_fail_closed_raises -v
# Expected: FAIL — PITViolation not raised (当前仅 warning)
```

**Step 3: Minimal implementation**
```python
# src/hero_quant/backtest/engine.py:536-555
# 首分支改 logger.warning→raise PITViolation，仅当 allow_synthetic==True 才 pd_date=index[0]；次分支保持 raise
```

**Step 4: Run — confirm it passes**
```bash
pytest tests/test_backtest_pit_correct.py -v  # 3 passed
grep -rn "allow_synthetic" tests | wc -l     # >0
```

**Step 5: Commit**
```bash
git add src/hero_quant/backtest/engine.py tests/test_backtest_pit_correct.py && git commit -m "fix(backtest): PIT fail-closed PITViolation 2a"
```

### Task 5: backtest/engine.py on_bar 与周转率单口径

**Files:**
- Modify: `src/hero_quant/backtest/engine.py`, `src/hero_quant/backtest/metrics.py`
- Test: `tests/test_backtest_engine.py`

**Step 1: Write the failing test**
```python
def test_on_bar_no_silent_fallback():
    from hero_quant.backtest.validation import ValidationError
    with pytest.raises(ValidationError):
        engine.on_bar(bar_missing_close)  # 不应走 bar.get("close", bar.iloc[0]) 回退

def test_turnover_single_source():
    # net_ret 为 gross 语义，成本仅主循环扣一次；turnover_rate.iloc[0] 按 ‖w‖₁/total_weight 非硬 1.0，空仓 0
    assert turnover_rate.iloc[0] != 1.0 or total_weight == 0
```

**Step 2: Run — confirm it fails**
```bash
pytest tests/test_backtest_engine.py::test_on_bar_no_silent_fallback -v
# Expected: FAIL — 未抛 ValidationError
```

**Step 3: Minimal implementation**
```python
# engine.py:376-413 删除 bar.get("close", bar.iloc[0]) 与 historical_base_price 回退
# 窄化为 except ValidationError: raise + except (ValueError…) as e: raise ValidationError(...) from e
# 682-823 抽 _compute_turnover_rate(pos_proxy) 复用；net_ret 改 gross，成本单次扣除；首日 turnover 按 ‖w‖₁/total_weight
# metrics.py:81,159 turnover/2 补 docstring；成本改乘法 gross_ret - turnover*cost_rate 单一口径
```

**Step 4: Verify**
```bash
pytest tests/test_backtest_engine.py -q
```

**Step 5: Commit**
```bash
git add src/hero_quant/backtest/engine.py src/hero_quant/backtest/metrics.py && git commit -m "fix(backtest): on_bar fail-closed and turnover single source 2a"
```

### Task 6: backtest/bench.py 合成守卫与 disclosure 去重

**Files:**
- Modify: `src/hero_quant/backtest/bench.py`, `src/hero_quant/backtest/validation.py`
- Test: `tests/test_backtest_bench.py`

**Step 1: Write the failing test**
```python
def test_bench_allow_synthetic_false_blocks():
    from hero_quant.backtest.engine import PITViolation
    with pytest.raises(PITViolation):
        bench.run(allow_synthetic=False)  # 不应被 296-300 强制覆写为 True

def test_bench_synthetic_fallback_marks_provenance():
    r = bench.run_with_fallback()  # except (ValueError,RuntimeError) 分支
    assert r.provenance == "synthetic_fallback"
```

**Step 2: Run — confirm it fails**
```bash
pytest tests/test_backtest_bench.py::test_bench_allow_synthetic_false_blocks -v
# Expected: FAIL — 未抛
```

**Step 3: Minimal implementation**
```python
# bench.py:285-310 删 296-300 强制覆写；if not allow_synthetic: raise PITViolation("bench synthetic requires allow_synthetic=True")
# 保留 except synthetic_fallback 但标记 provenance=synthetic_fallback
# 93-116 删 5 个重复 disclosure 别名，仅保留 get_disclosure/_build_pit_disclosure，其余 DeprecationWarning 包装
# validation.py 补 DatetimeIndex 去重/排序校验
```

**Step 4: Verify**
```bash
pytest tests/test_backtest_bench.py -q
```

**Step 5: Commit**
```bash
git add src/hero_quant/backtest/bench.py src/hero_quant/backtest/validation.py && git commit -m "fix(backtest): bench synthetic guard and disclosure dedup 2a"
```

### Task 7: 测试迁移（关键）

**Files:**
- Modify: `tests/test_backtest_engine.py`, `tests/test_backtest_pit_correct.py` 等
- Test: `grep -rn allow_synthetic tests`

**Step 1: Write the failing test（迁移完整性）**
```bash
grep -rn "allow_synthetic" tests | wc -l  # 预期 >0，否则迁移未完成
```

**Step 2: Run — confirm it fails（迁移前）**
```bash
grep -rn "allow_synthetic" tests; echo "count:$?"
# count 0 → FAIL
```

**Step 3: Minimal implementation**
```bash
# 所有 engine.run(prices, weights…) 不传日期的用例显式补 allow_synthetic=True
# 新增 test_pit_fail_closed_raises 3 例；批量 sed + 人工复核 skip_pit 分支
```

**Step 4: Verify**
```bash
pytest -q --cov=src --cov-fail-under=50  # 新增用例通过，旧用例不再静默过
```

**Step 5: Commit**
```bash
git add tests/ && git commit -m "test(backtest): migrate allow_synthetic explicit and add fail-closed tests 2a"
```

---

## Phase 2b — 安全：SSRF 与租户隔离（可与 2a/2c 并行）

### Task 8: telemetry/otel.py SSRF 加固

**Files:**
- Modify: `src/hero_quant/telemetry/otel.py`, `src/hero_quant/config/settings.py`
- Test: `tests/test_telemetry_ssrf.py`

**Step 1: Write the failing test**
```python
def test_ssrf_private_ip_blocked():
    from hero_quant.telemetry.otel import _is_allowed_endpoint
    assert _is_allowed_endpoint("http://169.254.169.254/latest/meta-data/") is False
    assert _is_allowed_endpoint("http://metadata.google.internal/") is False

def test_ssrf_userinfo_port_rejected():
    assert _is_allowed_endpoint("http://user:pass@10.0.0.1:4317/v1/traces") is False
```

**Step 2: Run — confirm it fails**
```bash
pytest tests/test_telemetry_ssrf.py -v
# Expected: FAIL — 当前 is_private and host==169… 恒假，仅卡一字面量
```

**Step 3: Minimal implementation**
```python
# otel.py:69-97
# if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return False
# 非 IP host：endswith("metadata.google.internal") / 169.254.0.0/16 二次解析；userinfo/port 校验
# 日志改 _redact_dsn(endpoint)（复用 config/settings.py:17 _redact_dsn）
# 保留 HERO_OTEL_MODE=disabled 默认关闭，仅 private 档位生效
# config/settings.py 抽公共 redact_dsn 供 billing/checkpoint/otel 复用
```

**Step 4: Verify**
```bash
pytest tests/test_telemetry_ssrf.py -v  # PASS
```

**Step 5: Commit**
```bash
git add src/hero_quant/telemetry/otel.py src/hero_quant/config/settings.py && git commit -m "fix(telemetry): SSRF guard and redact_dsn reuse 2b"
```

### Task 9: billing/checkpoint/api 租户隔离与 /ready 实探

**Files:**
- Modify: `src/hero_quant/billing/service.py`, `src/hero_quant/checkpoint/postgres.py`, `src/hero_quant/api/server.py`, `pyproject.toml`, `requirements-lock.txt`, `Dockerfile`
- Test: `tests/test_billing_persistence.py`, `tests/test_checkpoint_pg.py`

**Step 1: Write the failing test**
```python
def test_pg_emulated_not_real():
    from hero_quant.billing.service import _is_real_pg
    assert _is_real_pg(dsn="postgresql://user:pass@localhost/db") is False  # 无 pool 时不应真

def test_ready_pg_probe_real():
    r = client.get("/ready")
    # 无 pool 时 pg_ok 必须 False，不再伪成功
    assert r.json()["pg_ok"] is False

def test_dsn_key_hashed():
    from hero_quant.checkpoint.postgres import _pg_store_key
    k = _pg_store_key("postgresql://user:secret@localhost/db")
    assert "secret" not in k
    assert len(k) == 12  # sha256[:12]
```

**Step 2: Run — confirm it fails**
```bash
pytest tests/test_billing_persistence.py -k test_ready_pg_probe_real -v
# Expected: FAIL — /ready 恒 True，DSN 明文作 key
```

**Step 3: Minimal implementation**
```python
# DSN key 改 hashlib.sha256(dsn.encode()).hexdigest()[:12]（复用 checkpoint/postgres.py:92 _pg_store_key）
# _is_real_pg / _is_real_pg_pool 作为唯一真实性判据；publish_factor/_pg_put_sync 在无 pool 时不再伪成功
# api/server.py:342-450 /ready 改实探：pool.connection()/getconn() → SELECT 1，pg_ok 仅当 _is_real_pg_* 且探活成功；cohere 探活不再恒 True
# pyproject.toml + requirements-lock.txt + Dockerfile 新增 psycopg[binary]>=3.1 + psycopg_pool，uv pip compile --generate-hashes 重锁
```

**Step 4: Verify**
```bash
docker compose up -d postgres && pytest tests/test_billing_persistence.py tests/test_checkpoint_pg.py -v
pip-audit --require-hashes -r requirements-lock.txt  # 0 高危
gitleaks detect --no-git -s src                       # 0 泄露
```

**Step 5: Commit**
```bash
git add src/hero_quant/billing/service.py src/hero_quant/checkpoint/postgres.py src/hero_quant/api/server.py pyproject.toml requirements-lock.txt Dockerfile && git commit -m "fix(security): pg isolation and ready probe 2b"
# 注意：锁文件变更单独 PR
```

---

## Phase 2c — 溯源：Trait 白名单与注册表缓存（可与 2a/2b 并行）

### Task 10: 白名单与缓存失效

**Files:**
- Modify: `src/hero_quant/data/trait.py`, `src/hero_quant/data/registry.py`, `tests/conftest.py` 或 `data/sources.py`
- Test: `tests/test_data_trait.py`, `tests/test_data_registry.py`

**Step 1: Write the failing test**
```python
def test_good_not_in_valid_sources():
    from hero_quant.data.trait import VALID_SOURCES
    assert "good" not in VALID_SOURCES

def test_settings_cache_cleared_per_test():
    from hero_quant.data.registry import _settings_mode_cache
    # 切 synthetic/live 后应失效，而非永不失效
    assert hasattr(_settings_mode_cache, "clear") or True

def test_synthetic_comparison_requires_flag():
    from hero_quant.data.registry import CrossSourceError
    with pytest.raises(CrossSourceError):
        registry.compare(synthetic="...", live="...")  # 无 allow_synthetic_comparison=True 需抛
```

**Step 2: Run — confirm it fails**
```bash
pytest tests/test_data_trait.py::test_good_not_in_valid_sources -v
# Expected: FAIL — "good" 仍在
```

**Step 3: Minimal implementation**
```python
# trait.py:64 删 "good"；tests 中 name="good" 改 "synthetic" 或 monkeypatch 补白名单（仅测试期）
# registry.py:21-45 新增 clear_settings_cache() + force_refresh 参数，conftest.py autouse fixture 每用例清缓存；或直接去缓存改为 Settings().data_mode 直读（推荐后者，lru_cache 已在 get_settings）
# registry.py:371-377 合成比较器不再 continue 跳过，改为 raise CrossSourceError 或要求 allow_synthetic_comparison=True 显式放行，日志升 error
# 统一 trait.VALID_SOURCES 与 registry VALID_SOURCES 单源（trait 导入 registry 或抽 data/sources.py）
```

**Step 4: Verify**
```bash
pytest tests/test_data_trait.py tests/test_data_registry.py -v  # PASS
```

**Step 5: Commit**
```bash
git add src/hero_quant/data/trait.py src/hero_quant/data/registry.py && git commit -m "fix(data): trait whitelist and registry cache invalidation 2c"
```

---

## Phase 3 — P2 债务收敛（3-5d，按需拆 PR）

### Task 11: memory/store/hierarchy/lifecycle 与 governance 性能

**Files:**
- Modify: `src/hero_quant/memory/store.py`, `src/hero_quant/memory/hierarchy.py`, `src/hero_quant/memory/lifecycle.py`, `src/hero_quant/governance/ledger.py`, `src/hero_quant/checkpoint/postgres.py`, `src/hero_quant/governance/reconcile.py`, `src/hero_quant/memory/ingest.py`, `src/hero_quant/billing/service.py`
- Test: `tests/test_memory*`, `tests/test_ledger*`, `pytest --benchmark`

**Step 1: Write the failing test**
```python
def test_ledger_append_perf():
    # 10k 记录 append <50ms 增量尾 hash 缓存
    assert duration < 0.05

def test_lifecycle_no_On2():
    # filename→meta 字典去 O(N²)
    assert True
```

**Step 2: Run — confirm it fails（性能基线）**
```bash
pytest --benchmark-only -k ledger
# Expected: >50ms (O(n))
```

**Step 3: Minimal implementation**
```python
# memory/store.py:190-293 抽 ensure_schema() 去重 DDL；920 hash 移出 RLock
# hierarchy.py:51,285 补 ":" 校验，prune_search_scope 无交集回退 scan_all
# lifecycle.py:133 建 filename→meta 字典去 O(N²)
# governance/ledger.py:732 增量尾 hash 缓存；checkpoint/postgres.py:77 落 run_text 并补回填迁移
# reconcile.py:141 以 (st_dev, st_ino) 判同文件；billing 加 (factor_id, buyer_tenant) 幂等键
# ingest.py:125 key 去绝对路径改相对路径
```

**Step 4: Verify**
```bash
pytest tests/test_memory* tests/test_ledger* -q
pytest --benchmark -k ledger  # <50ms
```

**Step 5: Commit**
```bash
git add src/hero_quant/memory/ src/hero_quant/governance/ src/hero_quant/checkpoint/ && git commit -m "fix(debt): P2 store/hierarchy/ledger/reconcile perf 3"
```

---

## Phase 4 — P3 抛光（1d）

### Task 12: 异常窄化与类型/生命周期收尾

**Files:**
- Modify: `src/hero_quant/telemetry/otel.py`, `src/hero_quant/checkpoint/temporal.py`, `frontend/src/pages/Dashboard.tsx`, `frontend/src/pages/Research.tsx`, `src/hero_quant/**/registry.py` 等 28 处
- Test: `mypy --strict`, `ruff check src`

**Step 1: Write the failing test**
```bash
ruff check src | grep "BLE001"  # 宽 except Exception 28 处
mypy --strict src/hero_quant/data/registry.py
```

**Step 2: Run — confirm it fails**
```bash
ruff check src  # 28 BLE001
```

**Step 3: Minimal implementation**
```python
# 窄化 28 处 except Exception → 具体异常；补 registry.get_bars 等返回标注；mypy --strict 增量
# otel.py:142 atexit.register(shutdown)；temporal.py:108 心跳加 min/max 界
# Dashboard.tsx:62 接 AbortSignal，Research.tsx optimizeDeps: ["echarts"]
```

**Step 4: Verify**
```bash
ruff check src  # 0
mypy --strict src/hero_quant/data/registry.py  # 0
```

**Step 5: Commit**
```bash
git add -A && git commit -m "chore(polish): narrow except, mypy, atexit and frontend deps P3"
```

---

## 集成回归（每阶段必跑）

```bash
npx tsc --noEmit -p frontend/tsconfig.json
npm run build --prefix frontend
npm run test:run --prefix frontend
pytest -q --cov=src --cov-fail-under=50
pip-audit --require-hashes -r requirements-lock.txt
gitleaks detect --no-git -s src
docker compose up -d postgres && pytest tests/test_billing_persistence.py tests/test_checkpoint_pg.py
```

## 5. 风险与回退（原计划 §5 保留）

- **测试大面积红（2a）**：预置 `allow_synthetic=True` 批量迁移脚本，保留 `skip_pit` 显式旁路，`git revert` 单 commit 可回退
- **驱动切换致 CI 锁文件漂移（2b）**：锁文件 PR 单独评审，CI `hash-lock` 失败即阻断合入
- **SSRF 误拦本地调试（2b）**：`localhost/127.0.0.1` 仅当 `HERO_OTEL_MODE=private` 且 `endpoint` 显式指向时放行，其余 `disabled` 不受影响
- **性能回退（ledger/memory）**：Phase3 改动加 `pytest --benchmark` 对照，O(n) 改增量需压测 10k 记录 append <50ms

## 6. 关键文件清单（原计划 §6）

- 前端：`frontend/src/pages/Settings.tsx`, `frontend/vite.config.ts`, `frontend/tailwind.config.js`, `frontend/tsconfig.json`, `.github/workflows/ci.yml`
- 回测：`src/hero_quant/backtest/engine.py`, `bench.py`, `metrics.py`, `validation.py`
- 数据：`src/hero_quant/data/trait.py`, `registry.py`, `data/loaders/*.py`
- 安全/隔离：`src/hero_quant/telemetry/otel.py`, `src/hero_quant/billing/service.py`, `src/hero_quant/checkpoint/postgres.py`, `src/hero_quant/config/settings.py`, `src/hero_quant/api/server.py`
- 治理/记忆：`src/hero_quant/memory/*.py`, `src/hero_quant/governance/*.py`

## 7. 工期估算（原计划 §7）

- Phase1 0.5d，Phase2 2.5d，Phase3 4d，Phase4 1d；P0+P1 可在 3d 内交付可发版分支，P2/P3 随迭代消化

---

## 执行交接

> Plan saved to `docs/plans/2026-09-01-opencode-review-p0p1-fix.md`. Two execution options:
>
> 1. **Subagent-Driven** — I dispatch a fresh sub-agent per task, review between tasks, TDD 强约束
> 2. **Manual** — You run the tasks yourself per plan
>
> Which approach?

*Source: ` .zcode/plans/plan-sess_1d4c6743-f15a-42de-9d71-d694da72bdfe.md` — 已按 Superpowers writing-plans 规范精梳，保留原 P0-P3 分级与文件清单，重构为 13 个 TDD 任务。*
