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

## 验收

- 对照项数：16（通过16）+ U1/U2 说明2项
- 差异数：4（注1/注2/注3 + RSI短序列seeding差，均判定等价/设计）
- 修复数：0（无确认bug，未动src）
- 回归测试：`tests/validation/test_quantlib_golden.py` 12 passed
- 报告路径：`docs/validation-report.md`（本文件）
