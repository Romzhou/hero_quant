# 批次 16 进度报告（2026-11-02）

> 为什么有这个文件：微信端看不到长输出，故把本轮汇报落盘。
> 本文件与同目录其它文档一样**未提交**（批次红线：不写 git），在工作树里等合并。

## 0. 一句话现状

批次 16 的 A/B/C 三阶段全部完成：本地/CI 观测差异已消除（skipped 90 → 3）、会话级审计生产者已接、两项 PIT 调研已出结论。
**卡在一个需要你拍板的决策**：122-b 用 `year_month` 还是 `symbol` 分区（两份调研结论冲突）。
批次 17 尚未开始。

## 1. 阶段 A：本地 PG 对齐

**诊断（原文）**
- 本地容器：`llm-pg` / `postgres:16-alpine` / `100.114.54.26:5432`
- skip 判断：`tests/conftest.py` 把未显式导出的 `HERO_*_DSN` 置空，PG 守卫（DSN→TCP 0.3s→connect 2s）失败即整族 skip
- 解析顺序：`HERO_CHECKPOINT_DSN → HERO_PG_DSN → HERO_PG_HOST/USER/PASSWORD`（后者拼 `postgresql://user:pw@host:5432/heroquant`）
- CI：`test.yml` / `ci.yml` 都带 `postgres` service（原 `postgres:16`，现改 `postgres:16-alpine`），DSN `postgresql://postgres:postgres@localhost:5432/hero_quant`

**结果**

| 跑法 | 结果 |
|---|---|
| `pytest -q`（无 DSN，改动前） | 3581 passed / **90 skipped** |
| `scripts/test_local.sh -q`（注入 DSN） | **3675 passed / 3 skipped / 0 failed** |
| PG 族 | 63 → **66 passed** |

82 条首次在本机跑：**零失败**，因此"环境耦合 vs 真 bug"分类表为空。
剩余 3 条 skip 均合理：本机真 Redis ×2、Windows 专属路径 ×1。

**交付**：`scripts/test_local.sh`、`scripts/test_local.ps1`、`docs/local-development.md`、README 指引、CI 镜像 tag 对齐。
**明确未做**：没有在 conftest 里加自动探测；没有删断言/放宽/加 skip。

## 2. 阶段 B：PR-241 session.run 生产者

- `audit/producers.py` 新增 `session_run_hook()`：created/started/finished → `session.run`；finished 时 `ok = status=="succeeded"`；detail 只允许 phase/status；钩子自己吞异常。
- `session/store.py`：`SessionStore(pool, *, audit_hook=)` + `_notify_audit()`，在 `create_session` / `start_run` / `finish_run` 触发；`get_session_store()` 工厂默认注入。
- 测试：`tests/test_pr241_session_pg.py`（3 例真 PG，含"历史 NULL 行不判篡改、uncovered_count=1、chain_start_entry_id 指向首个哈希行"）+ `tests/test_pr241_session_run.py`（4 例离线）。
- 数字：全量 3668 → **3675 passed**；PG 63 → **66**。

**已知缺口（未修，属产品面）**：仓库只有 `GET /v1/sessions`、`GET /v1/sessions/{id}`、`POST /v1/sessions/{id}/resume` 三个只读端点；`create_session/start_run/finish_run` 没有调用方，所以线上不会产生 `session.run` 事件，直到有人实现会话写入端点。

## 3. 阶段 C：两项调研

### C1 查询形状（`docs/research/pit-query-shapes.md`）

| 口径 | 时序型 | 批量逐标的 | 真横截面 |
|---|---|---|---|
| 条数 | **8/10 = 80%** | 2/10 = 20% | **0** |
| 数据量权重 | 1686 行 = **99.5%** | 9 行 = 0.5% | 0 |

- 10 条路径全部实跑取数（synthetic 行数上限 + PIT 726 日分区样本）。
- 顺带发现真实缺陷：`PitParquetStore.as_of` 的 glob 不按 start/end 裁剪，**查 1 天也打开全部 726 个文件**（99.3ms）。
- 结论：保留 `tenant/symbol` 一级、时间二级从「日」粗化到「年/月」。

### C2 分区基准 + 数据质量（`docs/research/pit-partition-benchmark.md`）

规模：东财抽样 300 只（seed=42）× 2023-11-01~2026-10-30，腾讯 qfq，**201,973 行 / 284 只有数据 / 726 交易日**，12.6s，未降级。

| 方案 | 文件数 | 体积 | footer | 时序 冷/热 | 横截面 冷/热 |
|---|---|---|---|---|---|
| A trade_date（现状） | 726 | 14.60 MB | 25.6% | 399/369 ms（开 726 文件） | 69/71 ms（2） |
| B symbol | 284 | 4.87 MB | 7.9% | **25/27 ms**（2） | 148/98 ms（284） |
| C year_month | 36 | 4.94 MB | 4.4% | 36/27 ms（36） | **31/20 ms**（36） |

**数据质量**
- 缺失率：全样本 p50 0 / p90 1.24% / 最差 81.3%（新股 136 行）；窗口起点已上市 273 只 p90 0.69% / p99 2.20%；16/300 只 0 行。
- 停牌/退市：**无字段**（qfqday 仅 6 列）；volume==0 全样本 0 行，未冒充退市。
- 双源：**Yahoo 全端点 403**（3 域名 × 3 impersonate × 直连/代理）；改用东财对账：12 标的 × 726 日 qfq/qfq 中位 0.0000%、最大 5.2bp；qfq vs 不复权中位 1.54%、最大 55.67%。
- OHLC：30 行抽样全 PASS；**全量 201,973 行 0 违例**。
- 结论句：**价格基础可用，上市状态基础不可用**；前置条件=锁 qfq+factor_asof、另建 instrument master、0 行标的显式标记。

## 4. 需要你拍板：分区键（冲突点）

- C1 按代码形状主张 `symbol` 一级（今天 80% 是时序、0 横截面）。
- C2 按实测主张 `year_month`（两种形状都接近最优）。

**我的建议：year_month**。理由：
1. 时序只比 symbol 慢 11ms，横截面快 5 倍；roadmap 的因子/选股（PR-232/233）必然要横截面；
2. 文件数增长方向：symbol 随标的数（全市场 5000+ → 5000+ 文件），year_month 随时间（36/年）；
3. C1 的洞见不浪费：文件内按 `symbol,date` 排序 + symbol→row_group 索引，可同时保住时序裁剪。

未实测的组合（`symbol/year` 两级）会增加文件数（284×3），C2 未测，不建议拍脑袋用。

## 5. 立即生效项（已完成）

| 项 | 结果 |
|---|---|
| 缺 DSN 警告 | `tests/conftest.py` 的 PG GUARD 改横幅：明确指向 `scripts/test_local.sh` / `.ps1`，并说明"本地绿不代表 CI 绿" |
| 文档去行号 | `docs/` 全量替换 **936 处**（42 文件）`.py:123` → 只留文件名；现 `grep -rInE '\.py:[0-9]+' docs/` 为 0 |
| backlog 重算 L1–L4 | `docs/production-readiness/backlog.md` 新增章节：**L2 = 29/67**（原 42/67 作废），L3/L4 不主张；**L1 未接线 12 项**逐条列证据；PR-181 标"待复核" |

L1 12 项：PR-112 真模型 E2E、PR-121 快照、PR-123 质量门、**PR-132 工具幂等**、PR-142 Grafana、PR-143 告警、PR-144 SLO、PR-161~165 部署五项。

## 6. 工作树（未提交）

修改：`.github/workflows/{test,ci}.yml`、`README.md`、`scripts/run_pg_tests.py`、`src/hero_quant/audit/producers.py`、`src/hero_quant/session/store.py`、`tests/conftest.py`，以及 docs 下 34 个去行号文件。
新增：`docs/local-development.md`、`docs/contracts/pr-112-regression-gate.md`、`docs/research/{pit-query-shapes,pit-partition-benchmark}.md`、`scripts/test_local.{sh,ps1}`、`tests/test_pr241_session_{run,pg}.py`。
磁盘：**1.1G 可用**（C2 的 /tmp 已清理）。

## 7. 待你两件事

1. **122-b 分区键**：year_month（建议）/ symbol / 其它。
2. **是否开批次 17**：B4 dedup 接线（含"worker 崩溃重跑副作用计数==1"集成测试）→ wiring_integrity（真实装配非 mock）→ B3 memory 装配 → B2 数据源文档 → B1 session 标注。

## 8. 合并后注意

CI 镜像已改 `postgres:16-alpine` 但本批无法 push 验证。合并后盯第一次 CI：若红在字符串排序/大小写比较，是 musl collation 差异，**不要改测试**，改回 `postgres:16` 再判断。
## 9. 工作树清单（未提交，按目录分组）

> 全部改动都在工作树，未做任何 git 写操作。路径均为仓库相对路径。

### src/
- `src/hero_quant/audit/producers.py` —— 新增 `session_run_hook()`：created/started/finished → `session.run`，detail 只带 phase/status，钩子自吞异常。
- `src/hero_quant/session/store.py` —— `SessionStore(pool, *, audit_hook=)` + `_notify_audit()`；在 `create_session` / `start_run` / `finish_run` 触发；`get_session_store()` 默认注入钩子。

### tests/
- `tests/conftest.py` —— PG GUARD 由单行提示改为横幅，明确指向 `scripts/test_local.sh` / `.ps1`。
- `tests/test_pr241_session_run.py`（新）—— 4 例离线：phase 映射、detail 白名单、钩子吞异常、工厂注入。
- `tests/test_pr241_session_pg.py`（新）—— 3 例真 PG：生命周期入链可验、历史 NULL 行不判篡改、钩子失败不影响会话写入。

### scripts/
- `scripts/test_local.sh`（新）—— 本地全量唯一入口：复用 `run_pg_tests.py` 的 DSN 解析后跑 pytest，解析失败 exit 2。
- `scripts/test_local.ps1`（新）—— Windows 同逻辑。
- `scripts/run_pg_tests.py` —— `_PG_TESTS` 登记 `tests/test_pr241_session_pg.py`（63 → 66）。

### .github/
- `.github/workflows/test.yml` —— postgres 镜像 `16` → `16-alpine`（与本地一致；**未在 CI 验证**）。
- `.github/workflows/ci.yml` —— 同上。

### docs/（新增）
- `docs/local-development.md` —— 本地测试唯一入口、本机/CI 镜像与库名差异、PG 隔离机制、常见故障。
- `docs/contracts/pr-112-regression-gate.md` —— PR-112 回归门：成本按次数、延迟只记录、质量 0.70、发版前手动跑。
- `docs/contracts/pr-122b-pit-decision.md` —— 122-b 分区键定为 year_month + 两个前置（glob 裁剪、instrument master）。
- `docs/research/pit-query-shapes.md` —— 10 条取数路径盘点：时序 80%、横截面 0。
- `docs/research/pit-partition-benchmark.md` —— 300 标的 × 3 年三方案对照 + 数据质量四项。
- `docs/production-readiness/status-2026-11-02-batch16.md` —— 本文件：批次 16 全量快照。

### docs/（修改）
- `docs/production-readiness/progress-2026-10-29.md` —— 追加 §20（级别、两条事故、口径切换）。
- `docs/production-readiness/backlog.md` —— 新增「未接线/未生效清单（合并审查）」与 L1–L4 重算（L2 = 29/67）。
- `README.md` —— 加 `scripts/test_local.sh` 指引。
- 以下 33 个文件仅为**去掉行号引用**（`.py:123` → 文件名），无内容改写：
  `docs/SBOM.md`、`docs/adr/ADR-005.md`、`docs/adr/ADR-006.md`、`docs/adr/README.md`、`docs/agent-eval-real.md`、`docs/agent-eval.md`、`docs/api-contract.md`、`docs/arch/agent.md`、`docs/arch/billing.md`、`docs/arch/data.md`、`docs/arch/memory.md`、`docs/arch/quantlib.md`、`docs/arch/telemetry.md`、`docs/audit-2026-10-28.md`、`docs/contracts/batch-14.md`、`docs/fuzz-report.md`、`docs/issues.md`、`docs/mutation-audit.md`、`docs/mutation-report.md`、`docs/plan-2026-10-28-p0-fix.md`、`docs/plan-2026-10-28-round8.md`、`docs/production-readiness/README.md`、`docs/production-readiness/phase-1.md`、`docs/production-readiness/phase-2.md`、`docs/quant-semantics.md`、`docs/recon.md`、`docs/redteam-round2.md`、`docs/round6-report.md`、`docs/round8-report.md`、`docs/runbook.md`、`docs/security-audit.md`、`docs/slo.md`、`docs/validation-report.md`

### 根目录
- `AGENTS.md` —— 新增 §11「完成口径与接线规则」（L1–L4、接线测试三层、红线三条）。
