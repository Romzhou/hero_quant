# quantlib / 回测正确性交叉验证报告（Q线）

- 范围：`src/hero_quant/quantlib/indicators.py:1-243`（sma/ema/rsi/bollinger/macd/max_drawdown）、
  `src/hero_quant/quantlib/options.py:1-275`（bs_price/bs_greeks/implied_volatility）、
  `src/hero_quant/quantlib/polars_base.py`、`src/hero_quant/quantlib/rust.py`（桥接）、
  `src/hero_quant/backtest/metrics.py:1-302`（sharpe/max_drawdown/annual_return/turnover/compute_metrics）、
  `src/hero_quant/backtest/engine.py`（BacktestEngine.run）。
- 方法：pandas-ta / TA-Lib 在本机均未安装（`ModuleNotFoundError`），按任务要求改用**手算 golden +
  pandas 独立实现**做三方对照。fixture 写死小数组 + 期望值，回归锁见
  `tests/validation/test_quantlib_golden.py`（12 用例）。
- 结论：**16 个对照项全部通过，确认 bug 数 0，无需改 src**（Q线是全仓唯一允许改 quantlib 的线，
  本次无改动必要）。2 项说明见“未覆盖/缺失”。

## 对照表

| # | 指标/参数 | quantlib值 | 对照值 | 差异 | 结论 |
|---|-----------|-----------|--------|------|------|
| 1 | SMA w=3 on [1,2,3,4,5] | [nan,nan,2,3,4] | 手算 [nan,nan,2,3,4] | 0 | 等价 |
| 2 | EMA span=3 on [1..5] | [1,1.5,2.25,3.125,4.0625] | 手算 α=0.5 递推 | 0 | 等价（adjust=False，与TA-Lib一致） |
| 3 | RSI 上涨列→100 / 横盘→50 | 100.0 / 50.0 | 手算边界 | 0 | 等价 |
| 4 | RSI 混合序列 vs Wilder-SMA-seed独立实现（200点，尾部50点） | 见报告 | 独立Wilder实现 | maxabs 9.0e-05 | 等价（收敛；早期差为seeding方法差异，见注1） |
| 5 | BOLL w=3,k=2 on [1..5] | mid[2]=2,up[2]=4,lo[2]=0 | 手算 ddof=1 | 0 | 等价（pandas-ta口径；TA-Lib用总体std会差√((n-1)/n)因子，见注2） |
| 6 | MACD(2,3,2) on [1..8] | — | pandas ewm直接算式 | maxabs 0.0 | 等价 |
| 7 | MDD [100,110,105,120,90,100] | -0.25（两实现一致） | 手算 (90-120)/120 | 0 | 等价 |
| 8 | MDD 含NaN/零起点/全NaN | 两实现逐项一致 | 语义期望（忽略NaN/回落0） | 0 | 等价 |
| 9 | Sharpe 常数收益序列 | 1247.933… | 手算 mean/std·√252 | bit一致 | 等价；零波动→0 |
| 10 | CAGR [100→200] / [100,110,121] | 2^252-1 / 1.21^126-1 | 手算 | bit一致 | 等价 |
| 11 | Turnover w=[0,1,0]→0.5, 稳定权重→0 | — | 文档单边口径 sum\|Δ\|/2/n | 0 | 等价 |
| 12 | compute_metrics smoke | sharpe/ar/mdd/vol/cumret自洽 | sharpe_ratio等子函数复用 | 一致 | 等价 |
| 13 | BS put-call parity S=K=100,T=1,r=5%,σ=20% | c=10.45058,p=5.57353 | S-K·e^-rT | residual 0.0 | 等价 |
| 14 | IV roundtrip | 0.19999988 | 真值0.2 | 1.2e-07 | 等价（二分精度内） |
| 15 | Greeks delta | 0.63683065 | 手算N(d1) | bit一致 | 等价 |
| 16 | 引擎buy-hold [100,110,121] w=[1] costs=0 | equity [1.0,1.1,1.1] | naive close比 [1.0,1.1,1.21] | 末bar差一拍 | 等价（设计：次日执行语义，见注3） |

注1（RSI seeding）：`indicators.py:122-123` 用 `ewm(alpha=1/n, min_periods=1)` 从bar0起平滑；
经典Wilder/TA-Lib以前n个gain/loss的SMA做种。16点短序列尾差约2.0（72.51 vs 70.50），
200点序列尾部50点maxabs 9e-05 → 差异随时间洗出，非bug，属参数对齐说明项。

注2（BOLL std口径）：`indicators.py:174` 用 `rolling.std(ddof=1)`（样本std，与pandas-ta一致）；
TA-Lib BBANDS用总体std（ddof=0），两者恒差 `√((n-1)/n)` 因子（如n=20差约2.56%带宽）。
quantlib选择与pandas一致，可解释，非bug。

注3（引擎末bar）：`engine.py:206-317` `_align` 次日可执行价语义下，bar0决策→bar1(110)成交、
末bar按自身收盘盯市，equity [1.0,1.1,1.1] 自洽（买入110→盯市121即+10%）。
naive close复利 [1.0,1.1,1.21] 隐含bar0收盘即成交，违反PIT。属设计，非bug。

## 未覆盖/缺失

- U1 polars加速路径（`polars_base.sma_polars`）：本机无polars包，`ImportError`回落分支；
  代码检查 `rolling_mean(min_samples=n)` 与pandas min_periods=n对齐，未实测。待有vector环境补测。
- U2 ATR：任务书提及但quantlib无ATR实现（`__init__.py:4` 仅导出6项），属功能缺失非正确性bug，
  不修（超出“最小修”授权，且无对照基准）。

## 验收（M线汇总 2026-10-20；Q线原文保留见 git 历史）

- 对照项数：16（Q全过）+ 新增28用例（R 6 + U1 11 + U2 11）
- 差异数：Q 4说明项（注1/注2/注3 + RSI短序列seeding差，均判定等价/设计）+ U1 窗口/别名契约说明1项 + ATR NaN/Inf诚实置空说明（均非bug，见U1/U2补记）
- 修复数：R 1（`metrics.py:156-160` min_count=1，0.375 bug→0.5口径）；U2 功能新增1（Wilder ATR，与TA-Lib一致）；U1 0（src零改动，只读复核）
- 回归锁：`tests/validation/test_quantlib_golden.py` 12 passed + `test_turnover_parity.py` 6 + `test_polars_parity.py` 11（9 passed, 2 skipped：需真实polars的sma数值对照）+ `test_atr_golden.py` 11 passed
- 口径结论：`0.5` 正确（单边日均换手 `sum(|Δ|)/2/(n_valid-1)`，首日剔除），`0.375` 为DF路径稀释bug；详见 `docs/quant-semantics.md §1-2`
- 报告路径：`docs/validation-report.md`（本文件）

## R线补记（2026-10-20，turnover 口径统一 N→Q）

- 结论：`0.5` 为约定（`sum(|Δ|)/2/(n_valid-1)`，首日剔除），`0.375` 为 DF 路径
  `sum(axis=1)` 稀释 bug（首行 NaN→0 计入分母）。详见 `docs/quant-semantics.md §1-2`。
- 修复：`src/hero_quant/backtest/metrics.py:156-160` DF 路径加 `min_count=1`
  使首行保持 NaN 并被 `dropna` 剔除。单列 `[1,2,3,2]`：`0.375→0.5`；
  双列 `a=[1,2,3,2],b=[10,10,12,12]`：`0.625→0.8333…`；
  `[-inf,1,2,3]` DF：`0.333…→0.5`（与 Series 一致）。
- 回归：新增 `tests/validation/test_turnover_parity.py` 6 用例
 （DF↔Series↔weights 同输入同输出：单列/多列/脏首行/中部缺口/稳定零/单点）。
- 待办（N线）：`tests/mutation/test_mutation_metrics.py` 中
  `test_turnover_dataframe_paths`（0.375/0.625）与 `test_turnover_minus_inf_paths`
  （1/3）锁定的是旧稀释行为，需同步更新为 0.5/0.8333/0.5，本线无权改 `tests/mutation/**`。

## U1补记（2026-10-20，polars 路径 parity，src零改动）

- 范围：`src/hero_quant/quantlib/polars_base.py`（sma_polars/ema_polars/rsi_polars）
  vs `src/hero_quant/quantlib/indicators.py`（sma/ema/rsi）。只读复核，未改 src。
- 方法：手算 golden + pandas 独立实现三方对照。本机无 polars 包时 sma 数值对照
  skip，委托路径（ema/rsi）与 `_to_series/_validate_window` 接线仍实测；另用 Fake
  `pl` 模块验证 `rolling_mean(min_samples=n)` / `min_periods` 回落接线与索引/名称保留。
- 对照表（`tests/validation/test_polars_parity.py` 11用例，9 passed + 2 skipped）：

| # | 用例 | 对照 | 结果 |
|---|---|---|---|
| U1-1 | sma手算golden w=3 on [1..5]→[nan,nan,2,3,4] | pandas sma | 通过 |
| U1-2 | sma_polars==sma==golden（含扩展60点/别名n/自定义索引/超窗全NaN） | 需真实polars | skip×2（本机无polars包） |
| U1-3 | Fake pl接线 min_samples=n + 索引/名称保留 | pandas rolling语义 | 通过 |
| U1-4 | 旧版polars回落 min_periods重试 | 同上 | 通过 |
| U1-5 | 空序列短路 / 缺polars fail-visible（ImportError带vector安装指引） | 语义期望 | 通过×2 |
| U1-6 | ema_polars委托pandas：手算[1,1.5,2.25,3.125,4.0625]+别名/空/NaN/Inf | Q.ema | 通过（bit一致） |
| U1-7 | rsi_polars委托pandas：边界100/50 + 200点Wilder收敛尾部<1e-3 | 独立Wilder实现 | 通过 |
| U1-8 | _to_series两侧一致（close/equity/首列/list/Inf/空） | Q._to_series | 通过 |
| U1-9 | 窗口/别名契约说明项（None/冲突别名/多余位置参数） | 说明锁定 | 通过（非缺陷，防误修） |

- 结论：合法输入两侧一致；`min_samples=n` 与 pandas `min_periods=n` 对齐；
  窗口/别名差异属说明项（polars更严格抛错，indicators兼容旧口径取末值/忽略，J1已锁定），非bug。
- 回归：`tests/validation/test_polars_parity.py` 11用例（9 passed, 2 skipped）。

## U2补记（2026-10-20，Wilder ATR 新增）

- 范围：`src/hero_quant/quantlib/indicators.py:221-308`（`_extract_ohlc` + `atr`）、
  `src/hero_quant/quantlib/__init__.py:2,4`（导出atr）。commit `3e97fda`。
- 口径：TR=max(H-L, |H-prevC|, |L-prevC|)，TR[0]=H-L；ATR[n-1]=mean(TR[0:n])，
  之后 ATR[i]=(ATR[i-1]*(n-1)+TR[i])/n 递推 —— 与 Wilder/TA-Lib 一致。
  前 n-1 个为 NaN（诚实置空，不填0）；有效TR不足n个全NaN；NaN的TR对应ATR为NaN
  且不污染后续状态（状态沿用上一个有效ATR）；Inf→NaN；别名 n/window/span 等价。
- 对照表（`tests/validation/test_atr_golden.py` 11用例，全过，不依赖pandas-ta/TA-Lib）：

| # | 用例 | 期望 | 结果 |
|---|---|---|---|
| U2-1 | 5 bar手算 n=3：TR=[1.0,1.5,2.0,1.5,2.5]→[nan,nan,1.5,1.5,5.5/3] | 手算 | 通过 |
| U2-2 | TR锁定（period=1时ATR==TR）bar1=1.5/bar4=2.5 | 手算 | 通过 |
| U2-3 | DataFrame(high/low/close)与三序列分参一致 | 互相对照 | 通过 |
| U2-4 | 200 bar独立Wilder实现交叉 | maxabs==0.0 | 通过 |
| U2-5 | bars<period全NaN / 空序列len0 / 非法窗口抛错 / 长度不一致抛错 | 语义期望 | 通过×4 |
| U2-6 | 中间NaN不污染状态（seed沿用ATR[2]递推）/ Inf→NaN种子顺延 | 手算 | 通过×2 |
| U2-7 | 别名n/window/span等价 + 包导出atr | 契约 | 通过 |

- tool层：`src/hero_quant/tools/quantlib_tool.py:47` `SUPPORTED_INDICATORS` 未含atr，
  属后续wiring（本线不扩tool面，quantlib包级可用即可）。
- 回归：`tests/validation/test_atr_golden.py` 11 passed。

## W4补记（Round 6 波次 2，src 整体覆盖率 67.85% -> 70.36%，只补测试、src 零改动）

- 范围：`src/hero_quant/{backtest,quantlib,llm,agent}` 四块的未覆盖分支补测。
  新增 31 个测试文件、821 个用例（1 个因 Windows 无符号链接权限被 skip），
  **未改动任何 `src/**` 文件**（`git diff --stat` 为空，改动全部是新增 `tests/test_w4_*.py`）。
- 实测口径（本机无 PostgreSQL / 无 Docker / 无外网，见"环境归一化"）：
  - 基线：`uv run --frozen --extra dev python -m pytest -q --cov=src --cov-report=term`
    → `TOTAL 25183  8096  68%`，**Total coverage: 67.85%**（covered 17087 / 25183）
  - 最终：同命令 → `TOTAL 25183  7464  70%`，**Total coverage: 70.36%**（covered 17719 / 25183）
  - 净增 covered 语句 **+632**，四个目标目录合计 **+600**（分目录见下表）
  - 任务书里的 68.14% 在仓库内无出处（`git grep 68.14` 无命中），本节以自测值为准
- 四块目录覆盖率（covered/total）：

  | 目录 | stmts | 基线覆盖 | 最终覆盖 | 提升 | 新增 covered |
  |---|---|---|---|---|---|
  | `backtest/` | 1541 | 71.84% | 78.46% | +6.62pp | +102 |
  | `quantlib/` | 666 | 83.48% | 91.59% | +8.11pp | +54 |
  | `llm/` | 407 | 81.08% | 90.91% | +9.83pp | +40 |
  | `agent/` | 3624 | 67.80% | 78.95% | +11.15pp | +404 |

- 提升最大的单文件：`agent/trace.py` 62%→81%（+88）、`agent/embed.py` 67%→89%（+71）、
  `agent/loop.py` 60%→65%（+70）、`backtest/engine.py` 68%→74%（+55）、
  `quantlib/rust.py` 73%→89%（+28，Rust 加速分支用假扩展驱动）。
- 环境归一化（本机特有，基线/最终一致，不影响前后可比性）：
  - `.env` 里的 `HERO_API_KEY` / `OPENAI_API_KEY` / `COHERE_API_KEY` 与 4 个
    `HERO_*_DSN` 置为单个空格 `" "`：`load_dotenv(override=False)` 遇已存在 key 不覆盖，
    而所有 gate 都做 `.strip()`，故 `" "` ≡ "未配置"（与 CI 无 key/无 DSN 的行为一致）。
    原因：本机 `api.openai.com` 的 DNS 被劫持到黑洞 IP（TCP SYN 无响应），
    `ChatOpenAI` 无 timeout → pytest 无限挂起；本机也无 PostgreSQL 监听
    （`[::1]:5432` SYN 无响应）。
  - 5 个依赖真实 PG 的测试文件在本机无限挂起，逐文件扫描（75s/文件）确认后
    **基线与最终都用同样的 `--deselect` 排除**：
    `tests/test_0904_c1.py`、`tests/test_checkpoint_pg.py`、`tests/test_j8_loop_ckpt_gov.py`、
    `tests/test_retrieval_eval.py`、`tests/test_vector_maturity4.py`（共 44 个用例）。
    这 5 个文件覆盖的 `checkpoint/postgres.py`、`memory/store.py` pgvector 分支
    不属于本线四块目录，对四块口径影响可忽略。
- 门禁：`uv run --frozen --extra dev ruff check src` → `All checks passed!`（src 零改动）。

### W4 发现的 src 行为差异（只记录，未改 src）

| # | 位置 | 现象 | 影响 |
|---|---|---|---|
| W4-1 | `src/hero_quant/agent/context.py:205-208` | `_collapse(text, 0)` 注释写"避免空输出"，实际 `marker[:0]` 返回空串 | 零预算折叠返回空串而非标记；与注释意图不符（低危） |
| W4-2 | `src/hero_quant/backtest/metrics.py:141-146` | docstring 示例"w=[0,1,0] 时单边换手应为 1.0"，实现按日均口径返回 0.5 | 与同段后文"w=[0,1,0,1,0] 得 0.5"及 `docs/quant-semantics.md` 口径自相矛盾（文档缺陷） |
| W4-3 | `src/hero_quant/llm/factory.py:199-203` | `setattr(chat, "timeout", timeout)` 在 pydantic v2 模型上被拒（未声明字段） | chat 对象缺 `timeout` 内省属性；超时透传实际由 `LLMClient.timeout` + `_invoke_once` 签名探测保证，不影响功能 |
| W4-4 | `src/hero_quant/backtest/validation.py:53-58` | `weights_on/price_date/currency` 的 kwargs 兼容分支不可达（同名参数先被绑定，kwargs 永远取不到这三个 key） | 死代码（3 行） |

### W4 遗留台账

- `agent/loop.py` 仍有 528 条未覆盖语句（65%）：`run()` 内约 40 处 `except: pass` 级别的
  best-effort 兜底，需要逐个注入"双故障"才能触达，收益/成本比低，本波次未做。
- `quantlib/rust.py` 的真实 Rust 加速分支在本机不可达（未编译 `crates/` 扩展，
  `IS_RUST=False`）；本波次用假扩展驱动了全部加速路径的语句覆盖，真实扩展路径未验证。
- `agent/embed.py` 的 openai / sentence-transformers 真实网络调用未验证（本机无外网）；
  本波次用假模块驱动了协议层分支。
- `backtest/engine.py` 的持仓回落路径（`raw_positions_rows` 长度不匹配，engine.py:1032-1051）
  在正常流程中不可达（主循环每 Bar 必产出一行），未覆盖。
- `backtest/validation.py:54/56/58`（kwargs 兼容）与 `agent/graph.py:27-32`
  （`langgraph.types` 缺失回落 `langgraph.graph`）在本机不可达/为死代码，见 W4-4。
- `agent/context.py` 的 `_cached_auto_digest` 真机路径依赖 `skills/` 目录扫描，
  已用假 loader 覆盖异常分支，真实 digest 内容由既有 skills 用例覆盖。
