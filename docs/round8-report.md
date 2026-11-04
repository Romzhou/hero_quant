# Round 8 收口报告（正确性 · 可移植性 · 测试真实性）

> 时点：2026-10-29 · 基线：`951cd7d`（Round 7 后） · 终态：本文件所在提交
> 口径：数字均为本轮实跑或执行方交接原文；不转抄旧数据，引用处标注来源。
> 边界：本线只新建本文件 + 改 `CHANGELOG.md` / `pyproject.toml`（version）/ `docs/audit-2026-10-28.md`；`src/`、`tests/`、`migrations/` 零改动。

## 1. 提交清单（R8 四条线）

| 线 | hash | 标题（git 原文） | 一句话内容 |
|---|---|---|---|
| D / R8-1 | `bf137e6` | checkpoint 以 PG 为唯一真值… | 过期 checkpoint 不再回退 emulated；billing 跨进程真查库；新增非 superuser RLS 实证用例 |
| D / R8-1 | `170be1e` | 回测产物默认只吃真行情… | `allow_synthetic=False` + 显式 PIT 日期；拿不到返回空指标 + `degraded(backtest:no_market_data)`；缓存键 v3 |
| A / R8-2 | `336d9fb` | compose 去掉 heroquant-shared 外部网络… | 容器直连宿主 `100.114.54.26`；setup 脚本标 deprecated |
| A / R8-2 | `4365017` | README 部署段去掉 setup-shared-network.sh… | 新克隆一条命令起全栈；`.env.example` DSN 主机同步 |
| D / R8-1b | `a22f051` | 回测指标口径诚实化… | 响应新增 `sample/sharpe_basis/warnings`；<60 根 `sharpe=null`；无行情零样本；缓存键 v4 |
| B / R8-3 | `96ae7c5` | 第一批断言清理… | 13 个冗余源码锁删除/改写；并发、caplog、inspect 行为化 |
| B / R8-3 | `c765c4e` | 第二批断言清理… | 存储并发/索引产物/ingest sha256/阈值校验行为化；AUDIT 补状态列 |
| B / R8-3 | `454f072` | CI 固化 PG 守卫与 e2e 浏览器… | CI 装自带 chromium；`pytest -rs` 落盘并断言无 PG guard skip；skip 清单 |

## 2. 四条线交付

### R8-1 正确性（D 线）

| 项 | 结果 | 证据 |
|---|---|---|
| checkpoint 过期 | 有 PG 池时以 PG 为唯一真值，过期直接 `None`，不回退 emulated | `tests/test_checkpoint_pg.py` 无 `xfail` |
| billing 跨进程 | PG 模式 `list_factors/get_factor` 真查库；无池降级带 `degraded` | `src/hero_quant/billing/service.py`；交接实测 |
| RLS 实证 | 非 superuser 角色 `heroquant_app`；`HERO_RLS_TEST_DSN` 门控 | `tests/test_billing_rls_real.py`（3 例，默认 skip） |
| 回测产物 | 默认真行情，合成 demo 需 `HERO_BACKTEST_DEMO=1` | `src/hero_quant/api/server.py` 回测产物段 |

### R8-1b 指标口径（D 线）

- `<60` 根 K 线：`metrics.sharpe = null`（保留 key），`warnings=["insufficient_sample"]`，`sharpe_basis=null`；`cumulative_return/volatility/max_drawdown` 照旧。
- `>=60` 根：`sharpe` 数值 + `sharpe_basis="annualized_252"`。
- 无行情：`sample={"bars":0,"start":null,"end":null}` + `warnings=["no_market_data"]`，不编造日期。
- demo 模式同一规则，`markers.synthetic=true` 不变；缓存键 v4。
- 新增 `tests/test_backtest_metrics_disclosure.py` 4 例（20 根 / 120 根 / 无数据 / demo）。

### R8-2 可移植性（A 线）

- `docker-compose.yml` 已无 `heroquant-shared` 网络引用（仅注释保留决策记录），`docker compose config --services` 输出 `hero-quant`、`otel-collector`。
- 容器与宿主统一 `100.114.54.26`（tailscale IP）；换机器需改 `.env`。
- `scripts/init/setup-shared-network.sh` 保留但标 deprecated，README 不再引用。

### R8-3 测试真实性（B 线）

- 处理 28 个源码字符串断言函数：10 个删除（同文件/他文件已有运行时用例，或源码锁已随实现演进失效）、18 个改为运行时断言（并发只初始化一次、caplog 告警、`inspect.signature`/`iscoroutinefunction`、failing commit、`safe_load` 产物、直接断言模块常量）。
- 按统一 AST 口径，源码/文档 `read_text` 读取点为 **55**（其中纯源码结构锁 **48**，文档/迁移/配置契约 7），低于 60 目标；原审计的 134 处含测试产物读取，不可与本文口径直接相减。复算脚本见 `tests/AUDIT_source_string_assertions.md`。
- e2e 本地全链路：`npx playwright test` → **19 passed**，4 个 project 全部真调度（chat-sse / research-echarts / spa-routes / reconciliation）。
- CI：新增 `npx playwright install --with-deps chromium`；CI 下 Playwright 用自带 chromium（版本与 `@playwright/test` 对齐），本地仍优先系统 Chrome。
- CI PG 守卫：`pytest -rs` 落盘日志，新增步骤断言不得出现 `PG guard` skip，防止 47 个 PG 用例再次隐身。
- skip 清单：`tests/SKIP_INVENTORY.md`（47 PG 守卫 / 3 RLS opt-in / 2 真实 Redis / 1 Windows）。

## 3. 终态门禁（2026-10-29 实跑）

| 门 | 命令 | 结果 |
|---|---|---|
| 后端测试 + 覆盖率 | `.venv/bin/python -m pytest -q --cov=src --cov-fail-under=50` | **2762 passed / 53 skipped / 72.15%**，exit 0 |
| lint | `ruff check src` | **All checks passed** |
| checkpoint | `pytest tests/test_checkpoint_pg.py -q` | 13 skipped（无 PG DSN），**无 xfail** |
| 前端单测 | `npm run test -- --run`（frontend） | **70 passed / 10 files** |
| e2e | `npx playwright test` | **19 passed**（4 projects） |
| compose | `docker compose config --services` | `hero-quant` + `otel-collector` |
| 密钥扫描 | `grep -rn "sk-\|AKIA\|password=" --include=*.py --include=*.yml src/ .github/` | 仅命中 `security/redaction.py` 的正则定义，无真实密钥 |

skip 构成（53）：47 PG 守卫（未配 `HERO_CHECKPOINT_DSN`）、3 RLS（未配 `HERO_RLS_TEST_DSN`）、2 fuzz（环境配了真实 Redis）、1 Windows 语义。CI 有 postgres service，47 个 PG 守卫会真跑并由新步骤强制断言。

## 4. 未闭环 / 已知限制

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | 真行情固定只取 20 根（`_BACKTEST_BARS_TARGET=20`） | **未改，待产品决策** | 按 R8-1b 规则 `<60` 根不输出年化 Sharpe，因此默认真行情路径 `sharpe` 恒为 `null`。若要让 Sharpe 可用，需把目标根数提到 ≥60（改 `server.py` 常量与取样窗口），属新决策 |
| 2 | CI 首次 e2e 结果 | **待首次 GitHub Actions 验证** | 本地 19 passed；CI 改为自带 chromium，但尚未在 runner 上真跑过 |
| 3 | 3 条 RLS 用例 | 默认 skip | 需显式 `HERO_RLS_TEST_DSN`（非 superuser 角色）；CI 不提供 |
| 4 | 47 条 PG 用例本机 skip | 环境缺失 | 本机 `localhost:5432` 不通；CI 提供 service 后真跑 |
| 5 | `evals/run.py` 假数字残留 | 已知限制（R7 起） | 不在 R8 白名单 |
| 6 | `.codebuddy/` 未跟踪 | 工具产物 | 会话 worktree 目录；`.omc/` 同为未跟踪 |
| 7 | 前端 `metrics.sharpe` 为 null 的展示 | **E 线待办** | Research 页若直接展示 sharpe，需要显示 "—"；本轮不动 frontend |
| 8 | 包版本双源不一致 | **已知限制** | `pyproject.toml` 已升 0.3.0，但 `src/hero_quant/__init__.py` 的 `__version__` 仍为 0.2.0，`tests/test_bootstrap.py` 锁定该值；R8-4 白名单禁止改 `src/`、`tests/`，需下一轮同步 |

## 5. 推送前检查清单（未执行 push）

| 检查 | 命令 | 当前值 |
|---|---|---|
| 领先提交数 | `git log --oneline origin/master..master \| wc -l` | 70（含本文件提交） |
| 工作树 | `git status --short` | 仅 `?? .codebuddy/`、`?? .omc/` |
| 密钥扫描 | 见 §3 | 无真实密钥 |
| 分支 | `git branch --show-current` | `master` |
| 远端 | `origin` 存在但出网受限 | 推不推由总管决定 |

## 6. 复现命令

```bash
cd /root/projects/hero-quant
ruff check src
.venv/bin/python -m pytest -q --cov=src --cov-fail-under=50
.venv/bin/python -m pytest tests/test_checkpoint_pg.py -q
docker compose config --services
cd frontend && npm run test -- --run && cd ..
npx playwright test
```
