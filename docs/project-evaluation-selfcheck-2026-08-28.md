# hero-quant 自查报告 · 2026-08-28

> 基准：`docs/project-evaluation.md` (6.9/10) → `v2.md` (7.2/10) → `v3.md` (四维 7.5/5.5/7.5/4.0)  
> 复核方法：主线程 + 5 路并行 explorer 实测（`git HEAD=1d06ed4` + 工作区 39 文件未提交变更 `+2030/-606`）；所有 `file:line` 均为工作区实测。  
> 结论：**P0 链路 4 项已闭合 3.5 项**，项目从 v3 的“工程样板 + 断链”进入 **“可演示的准生产系统（demo-ready，仍不可直接资管）”**。

---

## 0. TL;DR

| 维度 | v1 | v2 | v3 | **本次自查** | 一句话 |
|---|---|---|---|---|---|
| 架构设计 | 8.5 | 8.5 | 7.5(闭环) | **8.7** | 11 控制点+双路由+工具合约仍是长板；`server.py` 接线后“孤立积木”感明显缓解 |
| 代码质量 | 8.0 | 8.2 | — | **8.4** | 删 `_is_legacy_caller`，诚实化改造多处；但 `FakeLLM`/`logger.debug silent` 包装仍有掩盖味 |
| 核心业务 | 6.0 | 6.3 | 7.5(闭环) | **7.3** | 回测多资产诚实化+数据默认 live+provenance 去伪是本次最大增量；仍缺 LLM 客户端层与 golden |
| 安全与供应链 | 8.5 | 8.8 | 7.5(工程) | **8.9** | CI 真阻塞+digest 锁定+SSE 票据+AST/Landlock 已是亮点分；`verify_hmac` 前缀检查仍是硬伤 |
| 测试与 CI | 7.5 | 7.8 | — | **7.9** | 277 passed / 49.5% / 门禁全阻塞真有效；但“薄覆盖+无 golden+无 Playwright”未动 |
| 产品闭环 | 3.5 | 3.8 | 4.0(业务) | **6.0** | `/v1/query/stream` 真接 `AgentLoop`，Chat/Research/Dashboard 已是真管道；Risk 仍静态、Monitor 未挂路由 |
| 生产就绪 | 5.0 | 5.2 | — | **6.2** | `live` 默认+诚实报错+PG/Temporal 骨架已搭；billing 内存态、鉴权弱、默认 `memory://` 未改 |
| **加权总分** | **6.9** | **7.2** | **—** | **≈ 7.6~7.8** | **从“最好的演示代码”跨到“最差的可演示系统”门槛；再补 RAG 接线+真 LLM 适配器+持久化三件即可摸 8.0** |

> 工作区未提交是本次得分跃升的唯一原因 —— `git status` 39 文件 `M`，`git diff --stat HEAD` 见 §1；若只看已提交 `1d06ed4`，分数仍停留在 v3 的 7.2。

---

## 1. 基线与变更面

```
HEAD: 1d06ed4 chore: clear source lint gate (2026-08-22 12:52)
工作区: 39 files changed, +2030/-606
  frontend/src/{App,Chat,Dashboard,Live,Research,Risk,Settings} + vite.config.ts  ~430
  src/hero_quant/api/server.py               +557  (接线主战场)
  src/hero_quant/backtest/{engine,metrics,validation} + tools/backtest  +684
  src/hero_quant/data/{registry,loaders/*}   ~150
  src/hero_quant/{checkpoint,governance,telemetry,mcp,memory} ~330
  tests/test_* 6 files  +38  (仅日志/断言微调)
```

**关键事实**：`git log` 自 `1d06ed4` 后零新提交；v3 的“零新提交、P0 仍开放”在已提交视角仍成立，但在**工作区**已大幅推进 —— 自查以工作区为准。

---

## 2. P0 闭环度复核（v1/v2/v3 的“不补则不可用”）

### P0-1 AgentLoop 接入 HTTP — ✅ 已闭合（半完美）

| 证据 | 结论 |
|---|---|
| `src/hero_quant/api/server.py:312-462` `GET /v1/query` 从 `return {"query":q,"status":"ok"}` 改为构造 `AgentLoop(llm,max_iterations=5,token_limit=60000,trace,ctx,grounding,graph,breaker,retry,replay_path,wall_time_budget,checkpoint=_get_checkpoint_saver())` (408) | 真接线 |
| `src/hero_quant/api/server.py:475-731` `GET /v1/query/stream` 票据门禁 `consume_ticket(ticket) else 403` (479)，`LLMFactory.model_for_stage("plan")` (326/497) → `ChatOpenAI(model,api_key,streaming=True)`，SSE 发 `tool/delta/need_approval/shadow/[DONE]` 真轨迹 | 真流式 |
| `server.py:346,517` `_FakeLLM` 兜底实现 `stream_chat/invoke/chat/__call__` 返回 `600519.SH close 1680.2...` 定值 | 离线可演示 |
| `server.py:79,99,113` 新增 `_get_checkpoint_saver/_get_shadow_stub/_log_mcp_status` 把 checkpoint/telemetry/shadow/mcp 8 模块尽力串起 | 积木已串 |

**仍缺**：无独立 `src/hero_quant/llm/client.py`。`llm/factory.py:1` 注释明写 *“does not create clients or make network calls”*，`llm/__init__.py` 仅导出 `catalog+factory`。真实网络调用是 `server.py:326` 内联 `ChatOpenAI(...,streaming=True,temperature=0.2)` **未传 `timeout`**（沿用 OpenAI SDK 默认 600s，与 v3 R1 “无 LLM 客户端层”同根）。`HERO_LLM_PROVIDER=deepseek/anthropic` 无适配器；`_call_llm` (`agent/loop.py:198` 鸭子分发 `stream_chat→invoke→chat→__call__`) 可用但重试/超时/usage 捕获未集中。

> 面试话术：*“闭环已接通、可用真模型流式跑通；但超时/重试/多 Provider 故障切换仍散在 `ChatOpenAI` 直调里，下一迭代会抽成 `llm/client.py` 统一层。”*

### P0-2 数据生产化 — ✅ 已闭合

| v3 缺口 | 本次 | 证据 |
|---|---|---|
| 默认 `synthetic` | **默认 `live`** | `src/hero_quant/config/settings.py:114` diff `-synthetic +live`，注释“禁止静默回退” |
| live 失败静默回退合成 | **显式报错** | `data/loaders/tencent.py:89-153`、`ccxt_loader.py:114-180`、`akshare_loader.py:147-199` 均改为 `raise RuntimeError/ImportError("pip install hero-quant[crypto]")`，不再 `return _synthetic_df` |
| provenance 伪造 `tencent` | **诚实标注** | `data/registry.py:260,276,290` `is_synthetic = _mode=="synthetic" or "synthetic" in lname` → `Provenance(source="synthetic" else _infer_loader_source)` |
| 跨源 1% 被 `except:pass` 吞 | **抛错+日志** | `registry.py:195 skip synthetic` + `:310 except Exception as e: logger.warning(...); raise CrossSourceError` |

**仍存小瑕**：`data/loaders/tencent.py:1` 模块 docstring 仍写“任意异常回退合成”（已过时）；`frontend` `Research` bench 曲线仍 `Math.random()*0.02`（见 §3）。

### P0-3 回测正确性 — ✅ 已闭合

- **`_is_legacy_caller` 已删**：`backtest/validation.py:12,20` diff 删除 `import inspect` + `def _is_legacy_caller(): inspect.stack() ... test_validation.py` + 分支 `if ts_w < ts_p and _is_legacy_caller(): raise`；现仅 `if ts_w > ts_p: raise ValidationError` (62)。
- **多资产诚实化**：`backtest/engine.py:303-348` 新增 `_price_matrix()` 按 `open/high/low/volume/currency` 分离非价格列；`run():470-503` `is_multi` 分支 `rets.pct_change()*w sum` (474-486) 替代 `close.pct_change()*leverage` 代理；`metrics.py:75` `turnover` 改 `diff.abs.sum(axis=1).mean()`；`tools/backtest.py:124-158` 逗号分隔 `AAPL,MSFT` 时每 ticker 独立 `DataFrame` + seeded `rng` 确定性合成。
- **tearsheet 去占位**：`engine.py:703-794` `_build_tearsheet+_drawdown_episodes_html` 月度 `ME` 热力 + Top3 drawdown 表，替换占位文案；`tests/test_validation.py:7` 新增 `PIT 等日有效、仅晚于触发` 用例。
- **仍缺 golden**：无 `tests/test_retrieval_eval.py` 式 golden 数据集；`sma_crossover` 两分支仍回 `equal_weight` (86-90)，`on_bar` 取到的 `aligned_price` 未参与定价 (`equity` 仍 `cum*(1+net_ret_close)` 569-576)，`Research` 热力/bench mock 见 §3。

### P0-4 推理层是洞（v3 R1）— ⚠️ 半闭合

- 已可真跑：`server.py` 真调 `ChatOpenAI` + 票据 + `AgentLoop` 全链路；`pytest` 277 全绿（含 VCR 回放）证明链路可跑。
- 仍缺独立适配器：无 `timeout`/`retry`/`usage` 集中层，`agent/embed.py:232` embedding 请求仍未传 `timeout`（600s 风险延续）；`factory` 仅选型。

**小结**：v1/v2 的三条 P0（接线/数据/legacy）在工作区已全部落地；v3 新增的“推理层是洞”完成 60%，剩下 40% 是抽层+超时重试。

---

## 3. 分维度细账

### 3.1 架构设计 8.7/10

**守住的长板**：`agent/loop.py` 11 控制点（`max_iterations=5/token_limit=60000/TRUNCATED/banner:462,651,1113/user_stop:479/wall_time_budget:265/0.8 折叠/BudgetBreaker/RetryPolicy`）、`graph.py` `StateGraph plan→Send→verify`、`policies.py` 指数退避+`$5/日` 滑动窗口、`grounding.py` 8 类掩码 + `frozen-identity` 4 类价格校验、配置单一入口 `config/settings.py:108` 13 处 `os.getenv` 收敛 + `test_config.py:80` 门禁。

**本次增量**：`server.py` 把 8 积木（checkpoint/telemetry/shadow/mcp/interaction/governance/llm/trace）通过 `_get_checkpoint_saver` 等尽力串起，双路由开关 `use_graph` 可经 HTTP 透传，`TraceWriter tmp→fsync→hardlink` + `BudgetBreaker` 真实开销熔断。

**仍松散**：`sandbox` Landlock 仍仅接 `sandbox/runner.py:236-349` 的 `python` 分支，`tools` 调度未走沙箱；`memory/skills` 未进 `loop`（见 3.2）。

### 3.2 RAG / Memory / Skills — 5.5 → 6.2（仍是短板，但比 v3 诚实）

**已扎实（工作区未改逻辑，仅加日志）**：
- 向量维度夹逼 `[8,2048]` 默认 32：`config/settings.py→agent/embed.py:23→memory/store.py:70,102,659,694` `_ensure_vector_dim` 重算、漂移回 `None`。
- pgvector 降级链：`memory/store.py:93-470` `PgVectorSidecar` 5s `connect_timeout` 熔断 → 本地 `vector` TEXT 列 → 文件扫描；`mcp/router.py:293` 同源 `is_pgvector_configured`。
- CJK：`store.py:29,606,1174` FTS5 trigram 主 + bigram 虚表兜底 + LIKE/file 回退（v2 `f2b61f4` 已修）。
- 生命周期：`memory/lifecycle.py:22,171` `HALF_LIFE 14d λ=ln2/14 ARCHIVE 0.15 DELETE 0.05 MIN_AGE 7 MAX_AGE 30` + `compress()` TF-IDF 日级/摘要。

**仍全缺（与 v3 一字不差）**：

| 简历级八问 | 现状 | 落点 |
|---|---|---|
| Memory 接入 Loop | **0 调用**：`agent/loop.py` 1429 行 `grep memory/recall` =0；`agent/context.py:208` `skills_digest=""` 硬编码，`SkillsLoader` (loader.py:16-167 两阶段扫描完整) 从未被 `loop` 调用 | `loop.py` 每轮 `recall` 注入 + `writeback` |
| 混合权重 | **两处不一致**：`mcp/router.py:347,418` `0.6*bm25+0.4*vec` vs `memory/store.py:1364` `0.6*cos+0.3*imp+0.1*bm25_hit` | 抽 `rank_fusion()` 统一 |
| RRF/重排 | **全仓 0 命中**：`grep RRF|rerank|cross-encoder` 仅 docs | `store.py:1265`/`router.py:315` 加 RRF+`ms-marco-MiniLM` |
| 评测 | **无 golden**：`grep recall@k|MRR` 仅 docs；无 `tests/test_retrieval_eval.py` | 30-50 对合成 golden + `recall@5` |
| 分块 | **无管线**：note 级原子写入，长文档无切 | `memory/ingest.py` 标题/段落+重叠 |
| 缓存 | **无**：embedding/检索零缓存，每次重算 | `lru_cache` + 结果缓存 |
| 维度依据 | 32 维无 ablation 文档 | 32/128/768 对比 |

**工作区变更**：`memory/store.py 170`、`mcp/router.py 29`、`lifecycle.py 20` 全是 `except:pass` → `logger.debug("silent handled: ...")` 包装（`patch_silent.py` 批量），**无 RAG 逻辑增量**。

> **风险提示（面试高危）**：若简历写“精通 RAG”，此维度必被追问。诚实答法：*“混合检索+降级+FST 已落地可用，但重排与评测是已知短板，权重拍脑袋、记忆未接线，下一步做 RRF+golden。”* — 反而加分。

### 3.3 工程落地 7.5 → 7.9

**已硬**：
- 可观测：`telemetry/circuit.py:35-195` 三态 `CLOSED→OPEN30s→HALF_OPEN5探→CLOSED` 双桶 50% 阈 + 30s 慢阈 + `DualBucketRateLimiter`；`telemetry/otel.py:15-68` 三模 `disabled/shared/private` + `BatchLogRecordProcessor→OTLPLogExporter` 回退 `urllib 0.5s`（工作区新增 fall-back）；`trace.py tmp→fsync→hardlink` 50k 侧车 + `/v1/trace/events` SSE 真流。
- 部署：`Dockerfile:4,17` digest 锁定 `node:22-slim@sha256:6c7479…`/`python:3.11-slim@sha256:e03112…` + `--require-hashes` (33) + 非 root 双用户 `vibe/vibe-sandbox 10001` (89) + `compose.yml:76,83 cap_drop+read_only+no-new-privileges`。
- 调度/Checkpoint 骨架：`checkpoint/postgres.py:78-530` `memory://` 默认 + 真 PG `min1/max5` + `expires_at TTL 7d` (20)；`checkpoint/temporal.py:18-191` 15s `HeartbeatHelper` 双写；`scheduled/service.py:30-374` ZoneInfo cron + 5 playbooks `08:30/15:30 CST 09:00 ET`。

**仍缺**：
- LLM 客户端层缺失（同 P0-4）。
- 鉴权弱：`api/security.py:93 verify_hmac` 仅 regex 前缀检查 `Bearer/sk-/AKIA/JWT` (125-132)，`check_host 86-87` 空白名单直接 `return True`，`verify_api_key 178-180` 未配置则 `True`；`HMAC compare_digest` 仅 bytes 路径 (145)。`server.py:32 _DEFAULT_LOOPBACK_HOSTS` 仅中间件层 403，`api/security.check_host` 仍放行。
- 持久化：`billing/service.py:11-128` 纯内存 `_factors/_purchases` + `tenant` 字符串过滤，无 PG/RLS；`checkpoint` 默认仍 `memory://`；`scheduled.dispatch` 注释“生产将入队 Temporal”无客户端。

### 3.4 安全与供应链 8.8 → 8.9

**硬化为真**：
- `requirements-lock.txt:1` `uv pip compile --generate-hashes` + `ci.yml:22 dry-run +54 real + test.yml:14` `--require-hashes` 阻塞；`test.yml:27 pip-audit +30 gitleaks-action@v2` 均阻塞（`|| true` 已删，见 v2 `9320c61`）。
- `Dockerfile` 三阶段+digest+hash 安装+非 root 已验证。
- `sandbox/ast_guard.py:155-189` `BANNED_IMPORT_ROOTS socket/subprocess/ctypes/requests + BANNED_ATTRS eval/__import__/os.system` fail-closed 深遍历；`sandbox/runner.py:197,304` Landlock `probe→unusable+require_enforcement→SandboxUnavailableError` 绝不执行；`sandbox/policy.py:12` `canonical_path resolve` 防穿越。
- `security/redaction.py:66` 按 sink 瀑布脱敏 `ARGUMENTS_SINK` 最严/`RESULT_SINK` 宽松 + `governance/ledger.py:455` 落盘前脱敏；`api/server.py:31 CSP default-src 'self' + X-Frame-Options` + `api/security.py:38` SSE 票据 `secrets.token_urlsafe 32 TTL60` 单次消费线程锁。

**仍弱**：见 3.3 鉴权；`sandbox/base.py:54` `bwrap` 不可用则 `no-op`，Windows `probe` synthetic `unusable` 且 `require_enforcement false` 时回退 `bwrap/no-op`；`security/approval.py:84` `ask→approved` 直通。

**工作区变更**：`governance/ledger.py 65`、`interaction/questions.py 9` 仅 `except:pass → logger.warning/debug` 包装，无逻辑变更（`patch_silent.py`）。

### 3.5 测试与 CI 7.8 → 7.9（量足质薄未变）

- **数量**：`pytest --collect-only` 277 (`tests/` 83 文件 220KB)，`pytest -q` 实测 `277 passed`（工作区 Windows 已无 v3 的 63 `sandbox 5` 受限；`pytest -k sandbox` 16 passed），`pytest --cov` 12223 stmts 6178 missed **49.46%** `Required 46%` 通过（v2 基线 10661 stmts 49%）。
- **质量**：分布双峰 —— `test_loop_maturity4.py 224L/12.9KB` vs `test_backtest_engine.py 9L/0.4KB` `test_agent_loop.py 9L/0.4KB` `test_quantlib.py 7L/0.2KB`，与 v1 “几百字节机制测试” 批评一致；前端 `frontend/src/__tests__/` 4 文件 11 tests `vitest` jsdom 全绿（`Chat 3t` 票据+EventSource、`Research 1t` tearsheet、`routes 6t`、`Monitor 1t`）；`tests/test_e2e.py:1-32` 仍是 Node fs mirror 假 e2e（`monkeypatch synthetic + FakeLLM + registry + BacktestEngine`），零 Playwright。
- **Golden 缺口**：`grep golden|numerical|oracle` 仅 `tests/test_stream_realtime.py:109 test_incremental_correctness_vs_full_sma`（增量 vs 全量 SMA，非真实行情金标准）；无 tearsheet/RSI 边界/回测指标 golden。
- **CI 真阻塞**：`ci.yml 40-60` 3 jobs + `test.yml 11-32` 5 门（hash/lint/pytest 46%/pip-audit/gitleaks）均阻塞，`pyproject.toml:43` 无 coverage 配置靠 CLI 注入，阈值 46 偏低（`+3% headroom`）。
- **工作区变更**：6 测试文件 `+38/-10` 仅 `HERO_DATA_MODE synthetic reload` + `prov.source tencent→synthetic` + `test_validation` PIT 等日用例修正，无新覆盖。

### 3.6 产品闭环 3.8 → 6.0（本次跃升主因）

| 页面/路由 | v3 | 本次工作区 | 证据 |
|---|---|---|---|
| **Chat** | 真管道接假后端 | **真管道接真闭环** | `frontend/src/pages/Chat.tsx:41-291` `POST /v1/query/ticket → GET /stream?q=&ticket=` + `EventSource` 主 + 1200ms 超时回退 `fetch ReadableStream` + `tool` 轨迹水位 + grounding/PIT 徽章 |
| **Research** | 部分 mock | **半真** | `Research.tsx:62-75` `csvIsMock/csvLoading/metricsLoading/tearsheetIsSynthetic` + `105-114 fetch /v1/backtest/{positions.csv,metrics.json,tearsheet.html}` 真产物；`29-59 parseCumulative` 真 CSV 解析；但 `121-176` bench 仍 `Math.random()*0.02`、`178-194` 热力回退硬编码 `5x7`、`X-HEAT` 写死 |
| **Dashboard** | 静态占位 | **半真** | `Dashboard.tsx:20-43` 真 fetch `metrics.json` 驱动 `annual_return/sharpe/mdd/turnover` + skeleton + `DEMO READY 30秒` CTA→`/backtest`；但总资产仍硬编码 `¥1.28M` |
| **Risk** | 静态 | **仍静态** | `Risk.tsx:1-90` `62%/20%/0.3%` 硬编码，注释“后续可接 /v1/risk/summary” |
| **Live** | SSE 轨迹 | **双路径+空态** | `Live.tsx:41-123` fetch + EventSource 增量 offset + paused 徽章；mock `0.28` 心跳保留 |
| **Settings** | 无 | **新增** | `Settings.tsx:17-37` 自检探针 `/live + /v1/backtest/metrics.json` 15s 轮询 Dot |
| **路由** | 6 路由 Monitor 未挂 | **7 路由** (`/dashboard/research/backtest|chat/live/risk/settings`)，`frontend/src/App.tsx:17-68`；`Monitor.tsx` 仍未挂载 | `App.tsx:59` |

**仍缺**：`Risk` 真接口、`Research` 热力/bench 去 mock、Dashboard 资产真实化、`Monitor` 路由挂载、浏览器级 Playwright 覆盖 `Chat 流式 + Research 渲染 + SPA`。

### 3.7 文档一致性 — 4 处失修已收敛 1 处

| v3 R5 失修 | 现状 | 是否修复 |
|---|---|---|
| README 徽章 `Tests-44 passed` | `README.md:12` 仍 `44_passed`，实际 `277` | ❌ 未修 |
| README “前端三页” vs `App.tsx` 6 路由 | `README.md:27` 仍“三页”，`App.tsx:17-68` 已 7 路由 | ❌ 未修 |
| `cp .env.example .env` 但无此文件 | `glob .env*` 仍 0，`README.md:53` 未改 | ❌ 未修 |
| CHANGELOG 0.2.0 “Billing RLS / Temporal Cron 5 playbooks” 夸大 | `CHANGELOG.md:20` 仍写 RLS + Cron + `Live <200ms WS`，实际 `billing` 内存桩+`scheduled` 占位+`Live` SSE | ❌ 未修 |
| 多处“数据默认 synthetic”文案 | `README.md:56,100` 仍 `synthetic`，但 `config/settings.py` 已切 `live` | ⚠️ 文案滞后代码（代码已诚实） |

---

## 4. 与 v3 增量题库的自测

> v3 §8 增量 12 题 RAG + 5 题业务，诚实作答即加分。节选最易露馅 3 题自测：

**Q1 混合权重 0.6/0.4 怎么来的？**  
答：拍脑袋，无离线评测。已在自查中标记统一 `rank_fusion` + golden `recall@k` 为 P1-1，当前两处权重不一致是已知债。

**Q2 为什么不用 RRF/重排？**  
答：当前线性加权可跑但无对比数据；全仓无 reranker 是事实短板，计划在 `memory/store.py:1265` 与 `mcp/router.py:315` 抽统一融合层 + `ms-marco-MiniLM` 本地重排，工作量 1 天。

**Q3 今天能给基金经理交付吗？**  
答：不能。诚实交底 P0 已闭但三处仍不可资管：① 无独立 LLM 客户端层（超时/重试/多 Provider 未集中）、② RAG 记忆未接线+无评测、③ billing/checkpoint 内存态。能交付的是“可演示的投研 Agent 原型 + 最硬的 30 秒 Grounding 拦截演示”。

---

## 5. 优先级补强清单（基于工作区增量重排）

### P0 — 摸 8.0 分的最后一公里（~2 天）

| # | 动作 | 落点 | 工作量 | 完成后面试叙事 |
|---|---|---|---|---|
| **P0-1** | 写真 LLM 适配器：抽 `src/hero_quant/llm/client.py` 统一 `openai/deepseek/anthropic` 流式 + `timeout(30s)+指数重试(3次)+usage 捕获+key 轮换`，`loop._call_llm` 只调它 | 新文件 + `server.py:326` 内联 `ChatOpenAI` 迁移 + `embed.py:232` 补 `timeout` | 0.5-1d | “推理层从图纸变可度量，超时/重试/失败率全链路” |
| **P0-2** | RAG 接线 + 去拍脑袋：`store.py`/`router.py` 抽 `rank_fusion` + `RRF + cross-encoder` + 30 对 golden `recall@k` 脚本 | `memory/store.py:1265` `mcp/router.py:315` + `tests/test_retrieval_eval.py` | 1d | “重排提升 X% 有数据支撑” |
| **P0-3** | 文档对齐：改 README 徽章 277/路由 7 枚/补 `.env.example`/CHANGELOG 去夸大/补 `Who is this for` + 双入口哲学 | `README.md:12,27,53,56,100` `CHANGELOG.md:20` | 0.5d 纯写作 | “文档即诚信分” |

### P1 — 拉开分差（~3 天）

| # | 动作 | 落点 |
|---|---|---|
| P1-1 | `loop.py` 每轮 `MemoryStore.recall` 注入 + `writeback`，`context.py:208` 透传 `skills_digest` | `agent/loop.py` + `context.py` |
| P1-2 | `billing/checkpoint` 落 PG：`billing` → `asyncpg` 持久化 + RLS，`checkpoint` 默认切 PG + `expires_at` 已备好 | `billing/service.py` `checkpoint/postgres.py:78` |
| P1-3 | `Research/Dashboard/Risk` 去 mock：热力/bench/资产/Risk 全接真实产物 `positions.csv/metrics.json/tearsheet.html` | `frontend/src/pages/Research.tsx:121,178 Dashboard.tsx:20 Risk.tsx:1` |
| P1-4 | 补 `SKILL.md` 实弹 + `memory/ingest.py` 长文档分块管线 | `skills/loader.py` + 新 `ingest.py` |

### P2 — 打磨

- P2-1 `pytest` Windows 兼容 `--basetemp` 指 workspace 内，消历史 63 受限（已在工作区消失，但仍值得加固）
- P2-2 Playwright 浏览器级 e2e：覆盖 `Chat 流式+Research 渲染+SPA 路由`
- P2-3 `pytest-benchmark` 实证 `surpass-design 5x/3x` 至少 1 项，或改文案为目标值
- P2-4 `ledger.py`/`tencent.py` docstring 去过时“回退合成”描述

---

## 6. Demo 话术（绕过剩余占位，以硬证据为主）

| 时间 | 动作 | 讲什么 |
|---|---|---|
| 0:00-0:40 | 口述+README | 一句话定位 + 四库对比 `TradingAgents 辩论 / Vibe 278k 全能 / hero 30k 微内核正确性闭环` |
| 0:40-1:40 | 架构 | `loop.py` 11 控制点 + `grounding.py` 三级反幻觉 + “批冻结身份” |
| 1:40-3:00 | 现场跑 `pytest -q` + `grounding` 单测 | 喂 `茅台 9999 元` 触发 `GroundingError` 拦截 —— 全场最硬 30 秒 |
| 3:00-4:10 | `docker compose up` → `:8899` | `Research` 真引擎产物 tearsheet；`Live` 真 `/v1/trace/events` 流 + 成本熔断条；`Chat` 票据→流式真闭环 |
| 4:10-5:00 | 诚实收尾 | 用本自查 P0 清单交底：LLM 适配器抽层 / RAG 重排评测 / 持久化三件已知债 |

**风险预案**：无 `HERO_API_KEY` 则 `FakeLLM` 离线演示全链路；live 网络不通则诚实披露 `provenance=synthetic` 且 `registry` 已不再伪造 `tencent`（v3 后已改四态披露）。

---

## 7. 证据索引（主线程实测）

- 接线：`src/hero_quant/api/server.py:79,99,113,312,326,346,408,465,475,479,497,517,575`
- 数据：`config/settings.py:108,114` `data/loaders/tencent.py:89,98` `ccxt_loader.py:114,120,175` `akshare_loader.py:147,153,194` `data/registry.py:195,244,260,276,290,310`
- 回测：`backtest/validation.py:12,20,62` `backtest/engine.py:25,86,233,273,303,470,513,558,569,703` `backtest/metrics.py:75` `tools/backtest.py:39,124`
- 记忆：`memory/store.py:29,56,70,93,102,606,659,694,707,1010,1174,1294` `memory/lifecycle.py:22,171,333` `agent/embed.py:23,252,299` `mcp/router.py:293,320,347,396` `agent/context.py:15,176,208` `agent/loop.py:198` `skills/loader.py:16` `glob SKILL.md 0`
- 工程：`telemetry/circuit.py:35` `telemetry/otel.py:15,85` `checkpoint/postgres.py:20,78` `checkpoint/temporal.py:18` `scheduled/service.py:30` `billing/service.py:16`
- 安全：`sandbox/ast_guard.py:18,155` `sandbox/runner.py:30,125,197,228,304` `security/redaction.py:15,66` `security/approval.py:17,84` `api/security.py:21,38,77,93,178` `governance/ledger.py:5,130`
- 测试：`pytest --collect-only 277` `--cov 12223 stmts 49.46%` `tests/test_e2e.py:1` `tests/test_stream_realtime.py:109` `ci.yml:22,54` `test.yml:14,27,30` `Dockerfile:4,17,33,89`
- 前端：`frontend/src/App.tsx:17,59` `Chat.tsx:41,115,154` `Research.tsx:29,62,105,121,178` `Dashboard.tsx:20` `Risk.tsx:1` `Live.tsx:41` `Settings.tsx:17`
- 文档：`README.md:12,27,53,56,100,27` `CHANGELOG.md:20` `git log 1d06ed4` `git diff --stat HEAD 39 files +2030/-606`

---

## 8. 一句话复盘

**v3 说“仍在组件打磨、未进入系统集成”—— 本次工作区已进入系统集成，且把最痛的 P0 三件（接线/数据/回测）做完了。**  
剩下的是 **“真 LLM 客户端层 + RAG 重排评测 + 持久化 + 文档诚实化”** 四件 P0/P1，做完即 8.0 分档、面试可硬答所有追问。

*自查人：主线程 + 5 路 explorer 并行审计；时间 2026-08-28；基线 commit `1d06ed4` + 工作区 39 文件未提交。*
