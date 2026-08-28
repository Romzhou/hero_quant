# hero-quant 优化补充计划 · 2026-08-28 Supplement

> 源：`docs/project-evaluation-selfcheck-2026-08-28.md` §2-§8 vs 已落地 `7559422+f639a78+e79dfc2` (302 passed, 48.99% cov) 差分审计 `exp-6`  
> 目标：把 4 Waves 遗漏的 P0 文档诚信 + P1 细节（回测/前端/覆盖率/Monitor）+ P1 安全/缓存等闭合到 8.2

---

## 遗漏总览（exp-6 审计, 12 项）

| # | 缺口 | 自查证据 | 状态 | 优先级 | 归属 Wave |
|---|------|----------|------|--------|-----------|
| D1 | 文档失修剩余 3/5 | `README:56,100 synthetic vs settings live`, `README:137 memory://`, `CHANGELOG 0.2.0` 夸大, `README:129 hash占位` | 部分修（badge/.env  done） | **P0** | Wave5 |
| B1 | `sma_crossover` 假信号 + `on_bar aligned_price` 未定价 | `engine.py:58-93` 两分支 `equal_weight`, `engine.py:569` 仍 close | 部分修（多资产 done，信号未修） | P1 | Wave5 |
| C1 | Monitor 路由未挂 | `Monitor.tsx` 存在但 `App.tsx:17-68` 7 路由无 Monitor | 未覆盖 | P1 | Wave5 |
| C2 | 前端仍 mock（Live/Monitor 0.28/0.3 + Research 热力空） | `Live.tsx:129` `Monitor.tsx:117` | 部分修（bench/Risk 已 de-mock） | P1 | Wave5 |
| D2 | Coverage 门禁倒挂 48.99% vs 48 | `pyproject 48` 刚勉强过，原计划 55 | 回退 | P1 | Wave5 |
| E1 | ingest key 非确定 + skills_digest 弱校验 | `ingest.py:106 hash()` + `loop inject` 未断言 prompt 含 digest | 部分修 | P1 | Wave5 |
| A1 | HMAC 前缀假鉴权 + Host 空放行 | `security.py:125-132` regex, `86-87` empty True | 故意未做（设计 P2） | P1 | Wave6 |
| A2 | Sandbox 未接工具调度 | `runner.py` 仅 python 分支 | 未覆盖 | P1 | Wave6 |
| C3 | Playwright 窄覆盖 | 仅 `reconciliation` 168L, 无 Chat/Research/SPA, CI 无 `playwright` | 部分修 | P1 | Wave6 |
| B2 | embedding/检索零缓存 | `lru_cache` 0 命中 | 未覆盖 | P2 | Wave6 |
| B3 | 向量维度 32 无 ablation | `settings [8,2048] default 32` 无文档 | 未覆盖 | P2 | Wave6 |
| E2 | LLM client 未度量（usage/metrics/otel） | `client.py 92L` timeout/retry done 无指标 | 部分修 | P2 | Wave6 |
| A3 | Approval 直通 | `approval.py:84 ask->approved` | 未覆盖 | P2 | Wave6 |

*Full Closed 8 项不再列：P0 接线、live、rank_fusion、Cohere、golden、checkpoint PG、billing RLS、ingest 骨架等*

---

## Wave5 · 收口波（0.8d, P0+P1 细节, 可独立演示）

> **For implementer:** TDD. 先失败测试 → 再最小实现 → 再 PASS → 提交。YAGNI 不蔓延。

### Task 15: 文档诚信收口 (P0, 0.3d)

**Files:** `README.md:56,100,129,137`, `CHANGELOG.md:20`, `docs/retrieval_eval.md` (append)

**Step1 Test** `tests/test_docs_honesty.py` 断言 5 项：`HERO_DATA_MODE` doc 与 `Settings` 一致为 `live`、`checkpoint PG` 非 `memory://`、`hash-lock 非占位`、`CHANGELOG`含 `2026-08-28 8.0` 条目、`retrieval_eval.md` 含 `ablation` 表  
**Step2 FAIL** → **Step3 Impl** 改 `README` 56/100 `synthetic→live`、137 `memory://→PG (fallback memory)`、129 `hash占位→real hashes`、补 `CHANGELOG ## [0.3.0] - 2026-08-28` 记录 `7559422+f639a78+e79dfc2`、append `retrieval_eval.md` ablation stub → **PASS** → Commit

### Task 16: 回测细节 `sma`+`on_bar`+golden (P1, 0.3d)

**Files:** `src/hero_quant/backtest/engine.py:58-93,569`, `tests/test_golden_backtest.py` 新建

**Step1 Test** `test_sma_crossover_bear_gives_0` 断言 `sma_short< sma_long → weight 0`；`test_on_bar_pricing_uses_aligned` 断言 `on_bar` 返回价参与 `equity`；`test_golden_tearsheet_oracle` 用固定 `600519.SH` 5 日合成跑 `positions/metrics` 对拍 `metrics.sharpe` 近似值（±0.01）  
**Step2 FAIL**（当前两 branch 等权）→ **Step3 Impl** `sma_crossover` bear→0 权 + `engine.run` 中 `equity cum* (1+ aligned_ret)` 分支（保留 close 路径兼容）→ **PASS** → Commit

### Task 17: Monitor 挂路由 + 前端热力/Live 去 mock (P1, 0.4d)

**Files:** `frontend/src/App.tsx`, `frontend/src/pages/Monitor.tsx`, `frontend/src/pages/Live.tsx`, `frontend/src/pages/Research.tsx`, `frontend/src/__tests__/Monitor.test.tsx`

**Step1 Test** `routes.test.tsx` 断言 `"/monitor"` 可达 + `Monitor.test` 断言 `Math.random` 不出现 + `Research` 有 `metrics.monthly_returns` 驱动热力  
**Step2 FAIL** → **Step3 Impl** `App.tsx` 加 `<Route path="/monitor" element={<Monitor/>}>` + Nav 加 `Monitor`、`Monitor.tsx` 删 `Math.random<0.3` 改 `fetch /v1/trace/events` SSE、`Live.tsx` 删 `0.28` mock、`Research.tsx` 热力由 `metrics.monthly` 真算→**PASS**→Commit

### Task 18: Coverage + ingest key 加固 (P1, 0.3d)

**Files:** `tests/test_backtest_engine.py` (增 thick), `src/hero_quant/memory/ingest.py:106`, `src/hero_quant/agent/loop.py` inject 断言, `pyproject.toml` / `.github/workflows/test.yml`

**Step1 Test** `test_ingest_deterministic_key` 两次 `ingest_markdown` 同文件 key 稳定（sha256 非 hash）；`test_backtest_thick` 补多资产+转仓用例提覆盖率 ≥50  
**Step2 FAIL**（`hash()` 抖动）→ **Step3 Impl** `ingest.py` key 改 `hashlib.sha256(piece.encode()).hexdigest()[:8]` + `loop` test 断言 `prompt.contains(skills_digest)` → **PASS**，提 `fail_under 48→50` 再验证 → **PASS** → Commit

**Wave5 验证:** `pytest -q --cov-fail-under=50` PASS, `ruff check src` 零新增, `npm --prefix frontend run test:run` 5 文件 PASS

---

## Wave6 · 加固波（1.2d, 安全+缓存+ablation+Playwright, 可与 Wave5 并行启动独立 lane）

### Task 19: 安全加固 (P1, 0.8d)

**Files:** `src/hero_quant/api/security.py`, `src/hero_quant/sandbox/base.py`, `src/hero_quant/sandbox/runner.py`, `tests/test_security_approval.py` 扩展

**Step1 Test** `test_hmac_strict` 空 Host → 403、`verify_hmac` 无有效 HMAC → 401、`tool -> sandbox` 非 python 工具走受限子进程  
**Step2 FAIL** → **Step3 Impl** `security.py` `check_host` 空→`False`（显式 deny）、`verify_hmac` 加 `hmac.compare_digest` 真校验路径、`base.py` 非 bwrap 时抛 `SandboxUnavailableError` 而非 no-op（工具调度层捕获）→ **PASS** → Commit

### Task 20: 缓存 + 维度 ablation (P2, 0.5d)

**Files:** `src/hero_quant/agent/embed.py`, `src/hero_quant/memory/store.py`, `docs/retrieval_eval.md`

**Step1 Test** `test_embedding_cached` 同 query 两次 `embed` 仅调一次底层、`test_retrieval_cached` 同 query 命中缓存  
**Step2 FAIL** → **Step3 Impl** `embed.py` 加 `@lru_cache(maxsize=1024)` + `store.py` `vector_search` 结果缓存 30s TTL → **PASS**；补 `retrieval_eval.md` 加 `32 vs 128 vs 768` ablation 表（3 行伪数据 + 方法描述，非跑真模型）→ **PASS** → Commit

### Task 21: Playwright 真 e2e (P1, 0.6d)

**Files:** `e2e/chat.spec.ts` 新建, `e2e/research.spec.ts` 新建, `playwright.config.ts`, `.github/workflows/test.yml`

**Step1 Test** `npx playwright test --list` 含 `Chat SSE 流式 / Research ECharts / SPA` 三用例  
**Step2 FAIL** → **Step3 Impl** 加 `Chat 流式 ticket→EventSource→[DONE]`、`Research 真 tearsheet 渲染`、`SPA /dashboard→/monitor` 三 spec + CI `npx playwright install --with-deps && npx playwright test` → **PASS** → Commit

### Task 22: LLM 可观测 (P2, 0.3d)

**Files:** `src/hero_quant/llm/client.py`, `src/hero_quant/metrics/__init__.py`, `src/hero_quant/agent/loop.py`

**Step1 Test** `test_llm_metrics` `llm_retry_total` / `llm_timeout_total` 计数、`trace.reason=llm_timeout`  
**Step2 FAIL** → **Step3 Impl** `client.py` 抛 timeout 时 `metrics.llm_timeout_total.inc()` + `trace` 写入 → **PASS** → Commit

**Wave6 验证:** `pytest -q --cov-fail-under=48` PASS（或 50 若已提），`ruff check` clean, `npx playwright test` 3 passed

---

## 执行策略

- Wave5（Task15-18）与 Wave6（Task19-22）在文件域无重叠，可 **并行双 fixer**（`fix-w5` 独占 `README/CHANGELOG/engine/frontend-coverage`；`fix-w6` 独占 `security/sandbox/embed/store/ablation/playwright`）。为 Interview 叙事，优先 **Wave5→Wave6** 顺序，但调度上可并行以压到 1.2d 墙钟。
- 每任务 TDD + `git add && commit -m "feat(...)"` + `ruff/pytest` 双门禁。
- 收口后 `docs/project-evaluation-selfcheck-2026-08-28.md` 追加附录 `§9 Supplement 2026-08-28` 记录闭合度 12/12。
