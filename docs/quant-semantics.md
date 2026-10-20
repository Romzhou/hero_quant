# quant 语义约定（R线锁定：N→Q turnover 口径差结论）

> 范围：`src/hero_quant/backtest/metrics.py:133-187`（turnover 标量）、
> `src/hero_quant/backtest/engine.py:319-369`（`_compute_turnover_rate` 逐 Bar 费率序列）、
> `src/hero_quant/quantlib/indicators.py:98-140`（RSI）、`:143-182`（BOLL）、
> `src/hero_quant/backtest/engine.py:206-317`（`_align` 次日执行）、
> `src/hero_quant/backtest/metrics.py:66-130`（CAGR 年化）。
> 依据：`docs/mutation-report.md §4` 顺带发现 1（0.375 vs 0.5）、
> `docs/validation-report.md` 注 1/2/3（RSI/BOLL/末 Bar）。

## 1. 结论先行：0.375 是 bug，0.5 是口径

- `0.5` 正确：单边日均换手 `sum(|Δ|)/2 / (n_valid-1)`，**不含首日**。
- `0.375` 是 bug：DataFrame 路径 `diff().abs().sum(axis=1)` 默认 `skipna=True`
  把首行全 NaN 的 diff 计为 `0.0` 并计入 `len`（分母 `n` 而非 `n-1`），稀释为
  `3/2/4=0.375`；同输入 Series/weights 路径经 `dropna` 后分母为 `n-1` 得 `0.5`。
- 同根第二例：`positions=[-inf,1,2,3]` 时 DF 路径 `dropna(how="all")` 吞掉首行
  全 NaN 得 `2/2/3≈0.333`，Series 路径得 `0.5` —— 同一病因（首日处理不一致）。
- 修复（`metrics.py:156-160`）：DF 路径 `sum(axis=1, min_count=1)` 使首行保持
  NaN 并被 `dropna` 剔除，与 Series/weights 一致。`min_count=1` 仅影响全 NaN 行
  （首行 diff），部分 NaN 行仍按有效值求和（多资产缺失快照不整行丢弃）。
- 影响：单列 DF `[1,2,3,2]` 由 `0.375 → 0.5`；双列
  `a=[1,2,3,2],b=[10,10,12,12]` 由 `0.625 → 0.8333…(=5/2/3)`；
  `[-inf,1,2,3]` DF 由 `0.333… → 0.5`。`tests/mutation` 中锁定旧稀释值
  （`test_turnover_dataframe_paths` 0.375/0.625、`test_turnover_minus_inf_paths` 1/3）
  的两个断言**已按本线口径更新**（`tests/mutation/test_mutation_metrics.py:139-144`
  现断言 0.5 / 5/6，`:292` 现断言 0.5；另见 `docs/mutation-audit.md` §3），
  旧稀释值断言已不存在。

## 2. turnover 统一口径（标量 `metrics.turnover`）

```
turnover = sum(|Δ|) / 2 / (n_valid - 1)
```

- **输入类型**：`positions` 优先。`DataFrame`（单列/多列）、`Series`、
  `list/ndarray`（经 `pd.Series` 归一）统一口径；`weights` 仅为无持仓时的回落
  （`positions is None` 才看 `weights`）。
- **清洗**：`to_numeric(coerce) + ±inf→NaN`；Series/weights 用 `dropna()` 剔除全部
  NaN；DataFrame 用 `dropna(how="all")` 仅剔除整行全 NaN（保留部分缺失行）。
  清洗后 `n_valid < 2`（单点/空）→ `0.0`。
- **分母**：`n_valid - 1` = diff 有效个数（`diff().dropna()` 长度）。**首日恒不计入**
  （首行 diff 为 NaN，必须剔除）。多资产先按行求和
  `sum(axis=1, min_count=1)` 再平均——仍是单边 `/2`（买卖双边 double-count 修正，
  见 `metrics.py:141-145`：`w=[0,1,0]` 时 `sum|Δ|=2`，单边为 `1.0`）。
- **脏输入 fail-closed**：`weights` 含任何非有限（NaN/±inf）直接 `0.0`，
  不清洗后计算（防脏权重伪装有效换手，`metrics.py:175`）。
- **与引擎逐 Bar 费率的区别**（易混点）：
  `engine._compute_turnover_rate` 返回的是**逐 Bar 费率序列**
  `pos_proxy.diff().abs().sum(axis=1).fillna(_init)` / `equity`，**首日含建仓**
  （`‖w‖₁/total_weight`，空仓为 0），用于 `run` 主循环按
  `turnover_rate[i]*costs` 逐 Bar 扣费；`metrics.turnover` 是**事后标量日均**
  （首日剔除），用于 tearsheet 展示。两者分母/首日语义不同是设计使然，不得混用。

## 3. RSI seeding 口径

- 实现：`indicators.py:122-123` `gain/loss.ewm(alpha=1/n, adjust=False,
  min_periods=1).mean()` 从 bar0 起平滑；`avg_loss==0 → 100`，
  全 flat（`avg_gain==avg_loss==0`）→ `50`；尾部 `fillna(50)+clip(0,100)`。
- 与 Wilder/TA-Lib 差异：经典以前 n 个 gain/loss 的 **SMA 做种**，本库用 EWM
  从 0 递推。短序列（16 点）尾差可达约 2.0（72.51 vs 70.50）；
  200 点序列尾部 50 点 `maxabs < 1e-4`（`test_rsi_converges_to_wilder_seed`），
  差异随时间洗出——**非 bug，属 seeding 说明项**。
- 新增守卫（Q 线已有）：有效点 `< n+1` 时返回全 NaN（不用 `50.0` 假中性误导）。

## 4. BOLL ddof 口径

- 实现：`indicators.py:172-174` `rolling(n, min_periods=n).mean/std(ddof=1)`，
  `upper/lower = mid ± k·std`，`k` 非法回落 `2.0`、负数回落 `2.0`；
  不足窗口保持 NaN。
- 与 TA-Lib 差异：TA-Lib `BBANDS` 用总体 std（`ddof=0`），本库/pandas-ta 用
  样本 std（`ddof=1`），恒差 `√((n-1)/n)` 因子（n=20 约 2.56% 带宽差）。
  例 `[1..5],w=3,k=2`：`mid[2]=2, up[2]=4, lo[2]=0`（`std([1,2,3],ddof=1)=1`）。
  **选择 ddof=1 是与 pandas 一致的口径声明，非 bug**。

## 5. 次日执行语义（PIT）

- `engine._align(idx)` 返回**次日可执行价**：有 open 用次日 open，否则次日 close；
  末 Bar 无次日，回落至当 Bar close/open（`engine.py:241-317`）。
- 主循环 `E[i]/E[i-1]` 即本 Bar 可执行收益，直接记到 bar i，不经 pending 缓冲
  到 i+1（`engine.py:845-957` 注释 double-delay 说明）。
- 推论：`buy-hold [100,110,121], w=[1], costs=0` 得 `equity [1.0,1.1,1.1]`
  （bar0 决策→bar1 以 110 成交 +10%，末 bar 按 121 盯市但不再复利一拍），
  naive close 复利 `[1.0,1.1,1.21]` 隐含 bar0 收盘即成交、违反 PIT。
  **末 bar 差一拍是设计，非 bug**（`validation-report.md` 注 3）。

## 6. 年化口径（CAGR）

- 公式：`(end/start)^(252/n) - 1`（`metrics.py:121`），252 交易日年化。
- `n` 为**原序列有效跨度** `_i1-_i0`（含内部 NaN 缺口，不用 `len(dropna)-1`；
  缺口消耗真实时间，不应缩短年化基数，`metrics.py:86-99`）。
  两点 `[100→200]` 得 `2^252-1`；`[100,110,121]` 得 `1.21^(252/2)-1`。
- 回落：`len<2 / start≈0 / ratio≤0或非有限 / n≤0` 一律 `0.0`（破产/尘埃起点
  不返回天文年化或 complex）。

## 7. 回归锁

- `tests/validation/test_turnover_parity.py`：DF↔Series↔weights 同输入同输出
  （单列/多列/脏首行/中部缺口/稳定零换手/单点），锁定本约定。
- `tests/validation/test_quantlib_golden.py`：12 用例（SMA/EMA/RSI/BOLL/MACD/
  MDD/Sharpe/CAGR/turnover 单边/BS/引擎次日语义）。
- 存量 `tests/mutation` 中 `0.375/0.625/1/3` 三个 DF 稀释断言**已更新**为本线口径
  （`tests/mutation/test_mutation_metrics.py:139-144`、`:292`），`pytest tests/mutation`
  现为绿（58 passed，实跑记录见 `docs/mutation-audit.md` §3）。

## 相关文档

本文件是量化口径的**唯一裁决源**（turnover 首日语义/RSI seeding/BOLL ddof/PIT 次日
执行/CAGR 跨度）；[docs/mutation-report.md](mutation-report.md) §4 的「顺带发现 1」
由本文件 §1 的结论转成 patch 建议，存活结论的独立复核见
[docs/mutation-audit.md](mutation-audit.md) §2 F1/F2。要改口径先看本文件，再去
mutation-report 确认该口径的回归锁是否已覆盖；只想看结论就直接看 §1。

- [docs/mutation-report.md §4 存活 mutant 按价值排序](mutation-report.md) — 存活 mutant 的等价性论证与「顺带发现」记账，本文件 §1 结论的上游观察源。
- [docs/mutation-audit.md §2 真缺陷清单](mutation-audit.md) — 对上一条等价性结论的独立复核（F1/F2 订正 id65/id81 表述）。
- [docs/fuzz-report.md §2 500 列表](fuzz-report.md) — API 侧 500 归零，与本文件同属「口径/降级必须显式」的契约族。
- [docs/agent-eval-real.md §1 结论](agent-eval-real.md) — 评测侧「无可用后端即留白」的 fail-closed 结论。
