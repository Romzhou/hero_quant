# N 线变异测试试点报告

> 范围: `src/hero_quant/quantlib` + `src/hero_quant/backtest` 相关 (`quantlib/indicators.py`,
> `backtest/metrics.py`, `backtest/validation.py`)。
> 约束遵守: 未修改任何 `src/` 文件与任何既有测试; 只新增 `tests/mutation/**` + 本报告;
> 未 `push`; 未读写 `.env`; `git add` 均为显式路径。

## 1. 方法与工具选型

- `mutmut 3.8` 已安装验证, 但其官方明确 **Windows 原生不支持** (`To run mutmut on
  Windows, please use the WSL`, issue #397), 本机无 WSL, 故采用等价轻量方案:
  TEMP 目录 (`C:\Users\Administrator\AppData\Local\Temp\opencode\mutation_harness/`,
  不进仓库) 中的 AST 变异 harness, 算子覆盖经典集合——
  AOR(`+ - * /` 互换)、ROR(六种比较符互换)、常数(`非零->0` / `零->1`)、
  单目符(`-x<->+x`)、Not 去除、`and<->or`、`min<->max`(含方法调用)。
  排除项: 注释/字符串、函数签名默认值、`logger.*` 实参、切片下标(只影响报错文案)。
- 每个 mutant 独立 `exec` + 跑 focused oracles(资金/信号/风控手算断言);
  判定: 断言失败或抛异常=杀死, 全过=存活, 编译失败=无效(本次为 0)。
- 基线跑(弱 oracle, 14+12+6 个) → 写 `tests/mutation/` 定向杀伤测试 →
  用**已提交的真实测试**对每个存活 mutant 重跑验证杀伤(内存 exec, 不碰 src)。

## 2. 分数

| 文件 | mutants | 基线杀死 | 新测试杀死 | 最终 | 最终 score |
|---|---|---|---|---|---|
| `backtest/metrics.py` | 291 | 136 | 76 | 212/291 | 72.9% |
| `quantlib/indicators.py` | 163 | 105 | 16 | 121/163 | 74.2% |
| `backtest/validation.py` | 84 | 45 | 32 | 77/84 | 91.7% |
| 合计 | 538 | 286 (53.2%) | 124 | **410/538** | **76.2%** |

- 新增测试: **57 个** (`tests/mutation/test_mutation_metrics.py` 35 个,
  `test_mutation_indicators.py` 9 个, `test_mutation_validation.py` 13 个)。
- 验证: `pytest tests/mutation -q` → 57 passed; `ruff check tests/mutation` → clean。
- 剩余存活 128 个, 全部归类为**等价 / 不可达防御代码 / 不值得**(第 4 节逐类给出证明或理由)。

## 3. 杀伤映射(测试 -> mutant, id 为 harness 内编号)

### metrics (76)
- `test_sharpe_risk_free_exact` → :33 `/->*`
- `test_sharpe_all_nan` → :24
- `test_sharpe_minus_inf_sanitized` + `test_compute_metrics_minus_inf_vol` → :22-UOR, :273-UOR, :245-or(id207)
- `test_max_drawdown_none` → :43-and
- `test_max_drawdown_unit_peak` → :57-const(`cummax!=1`)
- `test_max_drawdown_all_nan` → :58-fillna
- `test_max_drawdown_minus_inf` → :58-UOR
- `test_annual_two_point` → pin(两点 CAGR 按单期跨度年化 `2^252-1`, 精确锁定; 同边界由 declining 覆盖杀伤)
- `test_annual_declining` → :70-`<=`/`==`, :84-`<=`/`==`, :104-const, :114-const, :119-const
- `test_annual_gap_span` → :95-5 种 ROR
- `test_annual_gap_dropna_single` → :85(缺口去重后单点回落)
- `test_annual_trailing_gap` → :98-UOR(切片步长)、:98-AOR(`-`→`+` 第二处)
- `test_annual_leading_gap` → :99-AOR(`-`→`+`)
- `test_annual_bankrupt_end` → :119-`<`
- `test_annual_dust_start` → :284-or/const/dust、:289
- `test_annual_dust_pair` → :111-const(1e-12→0)
- `test_annual_overflow` → :127
- `test_turnover_dataframe_paths` → :156-:160 DF 分支 9 个(:157-const、:158-NOT、:159-6 个、:159-const)
- `test_turnover_weights_edge` → :183
- `test_turnover_weights_nonfinite_fail_closed` → :175-`<`/`<=`/`==`、:176
- `test_compute_metrics_empty_full_dict` → :230-:234
- `test_compute_metrics_costs_none_and_garbage` → :242、:244-const
- `test_compute_metrics_costs_exact_semantics` → :248-`*`、:250-`<`/`<=`/`==`、:256-4 个
- `test_compute_metrics_full_ruin` → :187、:250-(其余方向见等价类)、:256-clip、:261-`>`/`!=`/const
- `test_compute_metrics_zero_crossing_costs` → 方向钉(:247-UOR 经论证等价, 见 4 节)
- `test_compute_metrics_unit_costs` → :245-isclose-ref(`0`→`1`)
- `test_compute_metrics_two_point_costs` → :245-`>`/`!=`
- `test_compute_metrics_unit_scale` → :284-const(`start==1`)
- `test_compute_metrics_inf_start` → :284-or(原 dust_start 覆盖, 现独立)
- `test_compute_metrics_jitter_vol` → :276-const×2
- `test_compute_metrics_single_point` → :276-else、:281
- `test_compute_metrics_cum_overflow` → :287
- pin(未杀死但锁定行为): `test_turnover_positions_single`(:169 except 回落不可达)、`test_compute_metrics_*` 其余方向

### indicators (16)
- `test_to_series_dataframe_single_col` → :25-4 个(`>-><=`、`>->==`、`<`、`0->1`)
- `test_sma_window_one_identity` → :57-const
- `test_rsi_small_moves_threshold` → :119-const(`delta<1`)
- `test_bollinger_num_std_fallback`(+k=5 断言) → :160、:164-`>`、:164-`!=`、:165
- `test_macd_window_alias`(+fast=5 断言) → :194-5 个 ROR + :194-const
- pin: `test_ema_leading_nan_pinned`(:95/:206/:207/:209 等价)、
  `test_indicators_minus_inf_sanitized`(:43 等价)、
  `test_rsi_nan_positions_pinned`(:135/:137 不可达)

### validation (32)
- `test_pit_partial_args_skip` → pin(:102 等价, NaT 毒化, 见 4 节)
- `test_pit_mixed_same_instant_passes` → :131-or/`==`/`< =`/`>=`/const
- `test_pit_mixed_same_wall_east_rejected` → :119-`==`/`<`/`<=`、:128-`!=`/`<`/`>`
- `test_pit_mixed_same_wall_west_rejected` → :119-`>`/`>=`、:131-`>`
- `test_pit_mixed_valid_ordering_passes` → :128-`<=`
- `test_pit_mixed_west_wall_later_passes` → :128-`>=`
- `test_multi_asset_no_close_passes` → :146-or、:184-`>`/`>=`/`!=`
- `test_sibling_dirty_open_rejected` → :147-`==`/`<`/`<=`
- `test_sibling_dirty_adjclose_rejected` → :147-`>`/`>=`
- `test_sub_unit_prices_pass` → :157-const、:184-const
- `test_multi_asset_zero_price_rejected` → :184-and/`<`
- `test_multi_asset_negative_price_rejected` → :184-`==`
- `test_currency_lexicographic_mismatch_rejected` → :213-`<=`

## 4. 存活 mutant 按价值排序(全部 128 个的处置结论)

价值分级: P0=资金/风控/完整性(PIT、回撤、CAGR、成本、价格守卫),
P1=信号正确性(RSI/MACD/布林/换手口径), P2=统计细节/鲁棒契约, P3=病态输入。

### P0 剩余: 无可杀项, 全部等价(证明如下)
- `metrics.py:57` `cummax != 0 -> > / >=`(id50/51): 全负权益序列的 `dd >= 0` 恒成立证明——
  `cummax<=0` 的行满足 `s<=cummax<=0 ⇒ s/cummax>=1 ⇒ dd>=0`(更新行 `dd=0`), 而全局
  `min` 必 `<=0`(第 0 行 `dd=0` 或 where 置零), 故清零这些行不改变 `min`。证毕。
- `metrics.py:95 [CONST] 2->0`(id86, `len>=0` 恒进): 单有效点在非病态输入下不可达;
  病态 DF(首列全 NaN)下内层 `try` 吸收异常, `n` 回落值不变。等价。
- `metrics.py:259` 全 11 个(`<=`/`+`/`1`/`0` 变异, id237-246): 纯日志分支, 无状态变更。等价。
- `validation.py:102 [LOR] and->or`(id15): pandas3 `Timestamp(None)=NaT`,
  NaT 比较恒为 False, 额外进入的分支永不抛错。等价(已实证); 测试保留为契约 pin。

### P1 剩余: 全部等价/死代码(证明如下)
- `metrics.py:26 [CONST] ddof 1->0`(id10): 该 `_std` 只用于零值守卫, 不进入公式;
  `std(ddof=0)≈0 ⟺ std(ddof=1)≈0`(同零点; 单点时两者分别走 nan/0→同归 0)。等价。
- `metrics.py:36 [LOR] and`(id30)/`:37`(id31): `and` mutant 全路径同 verdict(逐分支演算); `:37`
  的 nan 守卫不可达(零波动已被 30 行拦截, inf 经清洗)。等价。
- `metrics.py:127`(id147 作废说明: 已杀)——更正: `:129 [CONST]`(id147):
  `ratio**exp` 上溢走 `OverflowError` 分支(123 行), inf 无异常路径不可达。等价。
- `metrics.py:247` fillna(0→1)×2 + UOR(id217-219): (a) fillna mutant 被首 Bar restore
  吸收为纯尺度变化, 全部六个指标都是比率型(尺度不变)。(b) UOR(`-inf` 不洗):
  costs 路径的 `+inf` 要求前一 Bar 恰为 0, 而 `X→0(X≠0)` 必是 `-100%` 当 Bar,
  导致 `(1+net)<=0` 截断清零 cumprod(或首 Bar 零起点全零权益), 变异不可观测。等价。
- `metrics.py:256 [AOR] *->/`(id231): 净权益整体除以 `s.iloc[0]` 是纯尺度变化,
  六个指标全为比率/差分型。等价(已实证 mutant 输出与原值完全一致)。
- `metrics.py:250 [ROR] >=->> / ->!=` + `[CONST] 0->1`(id223/227/228):
  成本路径恒 `len(net_ret)>=2`(受 245 行守卫), 三 mutant 永不改变分支。等价。
- `metrics.py:261 [LOR] or`(id247)/`[UOR] iloc[-1]->[+1]`(id255):
  costs 路径的 `net_equity` 永不为空(首 Bar restore 保证), `or` 短路恒真分支一致;
  `iloc[+1]` 需长度 1, 与 `len>=2` 矛盾。等价。
- `metrics.py:277` 五项(id263-266,271): 第二个 `sqrt(252)` 只在 `isfinite` 守卫内,
  `isfinite(_std*c)` 与 `isfinite(_std*0)` 对有限/零/inf/nan 四类输入 verdict 一致
  (已逐类实证); `else 1.0` 要求 `_std` 有限而乘积非有限, 不可能。等价。
- `indicators.py:122/123 [CONST] 1->0`(id48/53): 存活的是 `min_periods` 出现,
  非 `alpha` 分子(`alpha=0` 直接抛 ValueError, 已被基线杀死);
  `min_periods 1 vs 0` 在含前导 NaN 下输出逐位一致(已实证)。等价。
- `indicators.py:129 [ROR] !=->> / ->>=`(id72/73): `avg_loss` 为 ewm 非负输出,
  `> ⟺ !=` 恒成立; `>=` 恒真但 `loss==0` 行的计算值恰为 100, 与回落值一致。等价。
- `indicators.py:135/137`(id89-91): RSI 的 `gain/loss` 经 `where(...,0.0)` 永不为 NaN,
  ewm 有限输入给出有限输出(0/0 仅出现在 flat, 已被 flat_mask 先处理),
  故两处 `fillna(50)` 与 `replace→nan→fillna` 链不可达。等价(防御性死代码)。
- `indicators.py:179-181/213-215 [UOR]`(id116-118/138-140):
  `_to_series` 已把 ±inf→NaN, rolling/ewm 在本 pandas 版本把残余 inf 当缺失,
  输出端不可能出现 -inf。等价(已实证)。
- `indicators.py:233 [UOR/CONST]`(id150/151): `fillna` 只影响全 NaN-dd 情形,
  此时 `min=fill` 再经 `mdd>0` 钳位恒得 0。等价。
- `indicators.py:237/238/239/241/242`(id153-162): `dd[0]` 恒为 0(或 where 置零),
  故 `min<=0` 恒成立, `mdd>0` 钳位与 nan/inf 守卫永不触发。等价(死守卫)。
- `indicators.py:43 [UOR]`(id6): 同上 rolling/ewm 掩蔽, `-inf` 不泄漏(已实证
  sma/ema/rsi/macd 四路)。等价; 测试保留为回归 pin。

### P2 剩余: 契约边界等价 + 不值得
- `metrics.py:18`(id1/4/6)、`:70`(id61/67): 长度守卫的 `<=`/`==`/`2->0` 变异——
  两点/单点/空输入在下游(空 ret→0、单 ret std nan→0)收敛到同一 verdict。等价。
- `metrics.py:43`(id34/35/38): `len==0` 的 `<`/`<=`/`0->1`——空序列下游同样归零。等价。
- `metrics.py:60/61/62`(id58-60): `dd.empty else` 分支不可达(43 行已拦截空);
  nan/inf 守卫防御性(见 P1 论证)。等价。
- `metrics.py:83/84-const/85/105/110/115`(id69/75/110/112/125): dropna 后单点/空与
  float 转换异常分支——或下游收敛一致, 或 `to_numeric(coerce)` 永不抛错。等价/不值得。
- `metrics.py:94`(id77-80)、`:103`(id100-103): `n=len(s)-1` 两处默认值——仅在
  `_pos<2`(要求首列全 NaN 但 equity 列有效)或外层 try 抛错(实际不可达)时使用。P3 不值得。
- `metrics.py:99-const`(id99, `max(x,1)->max(x,0)`): `_i1>=_i0` 恒成立, `x>=1`。等价。
- `metrics.py:104/114 [ROR] <=->< / ->==`(id104/107/119/122): `n<=0` 不可达
  (`n>=1` 或早回)。等价。
- `metrics.py:111` 已杀; `metrics.py:160/166/182`(id158/168/194):
  三元 `else 0.0` 要求 `_m` 非有限——`sum(|diff|)/2/len` 经清洗后恒有限。等价。
- `metrics.py:169/186/292/293`(id169/196/289/290): except 回落/末端回落——
  `to_numeric/pct/diff` 在此路径不抛; 单点持仓测试保留为 pin。等价/不值得。
- `metrics.py:175 [ROR] >= / !=` + `[CONST]`(id171/175/176)、`:178` 三项(id180/184/185):
  逐类演算: 空/单点/脏权重下与原分支 verdict 一致(脏权重由 `all-finite` 守卫先行)。等价。
- `metrics.py:177 [UOR]`(id179): 权重 `-inf` 先被 175 行守卫拦截, 到 replace 时已返回。等价。
- `metrics.py:219 [NOT]`(id198): Series 输入包一层拷贝 vs 非 Series 跳过包装——
  `to_numeric` 对两者结果一致。等价。
- `metrics.py:245 [CONST] 1e-12->0`(id210): 1e-13 量级成本在容差内不可观测。容差, 不 pin。
- `metrics.py:245 [CONST] 2->0`(id216): 退化单点序列上下游同归零。等价。
- `indicators.py:25 [ROR] >= / !=`(id0/4): `shape[1]` 无负值/零列不可能。等价。
- `indicators.py:57 [ROR] ==`(id10): 负窗口原实现与 pandas 均抛 ValueError, 仅文案不同。等价。
- `indicators.py:74/91/107/155/197/198/199`(id13/14/16/94/126-128):
  `_validate_window(..., default=...)` 的 `default` 形参在函数体内**从未被使用**——
  死参数。等价(给 Q 线的清理建议见第 5 节)。
- `indicators.py:118 [ROR] >=`(id29)、`:119 [ROR] <=`(id37)、`:131 ×2`(id78/84):
  零增量在 gain/loss 置零语义下恒等。等价。
- `indicators.py:129 [CONST] 0->1`(id74, `avg_loss != 1`): 需 ewm 恰为 1.0, 特设输入。不值得。
- `validation.py:64`(id7)、`:208`(id69)、`:213 ×2`(id72/76):
  `len>0`/`nuniq>1` 定义域下 `!=`/`>=` 与原比较符恒等(长度/基数不可能为 0/负)。等价。
- `validation.py:123/125`(id21/22): `utcoffset()` 抛错回落, 触发需 tz 实现抛错。不值得。

### 顺带发现(未改 src, 记账给对应专线)
1. `turnover` DataFrame 路径 vs Series 路径稀释口径不一致: DF 路径 `sum(axis=1, skipna)`
   把首行 NaN-diff 计为 0 并计入 `len`(得 0.375), Series 路径 `dropna` 后得 0.5;
   且 DF 路径 `dropna(how="all")` 会吞掉全 NaN 首行(得 0.333)。行为均已用测试锁定,
   口径统一留给 Q 线决策。(P1 观察)
2. `max_drawdown` 对全负权益序列恒返回 0.0(负数域下 `s/cummax-1>=0` 的数学后果),
   `mdd>0` 钳位实际不可达。语义确认留给 Q 线。(P1 观察)
3. `_validate_window(..., default=...)` 的 `default` 参数从未被读取, 建议删除。(P2 清理)
4. `rsi` 两处 `fillna(50)` 与 `compute_metrics` 部分 `else 0.0` 为防御性死代码,
   实际由上游 `where`/`isfinite` 守卫覆盖。保留无害, 删除需 Q 线确认。(P2 清理)
5. `compute_metrics(costs=0)` 与跳过成本分支完全等价(零起点序列除外, 已有测试锁定)。(P2 结论)

## 5. 给 Q 线的 patch 建议(本专线未改 src, 仅记账)

- 如需统一换手口径: 二选一——(a) DF 路径 `sum(axis=1)` 改 `min_count=1` 使首行保持 NaN
  并被 `dropna` 剔除 (与 Series 一致得 0.5); (b) 接受稀释并文档化。影响 `metrics.py:157`。
- 如需删除死参数: `_validate_window(window, default=...)` 去掉 `default`
  (`indicators.py:47-60`, 调用点 :74/91/107/155/197/198/199 同步)。
- 负权益回撤语义: 在 `max_drawdown` 文档串中声明“全负权益返回 0.0”, 或改为按
  `s/cummax-1` 原值返回(将改变 :57 mutant 的等价性, 需同步更新本报告测试)。

## 6. 验收输出

- mutation score: 基线 53.2% (286/538) → **最终 76.2% (410/538)**;
  分文件 72.9% / 74.2% / 91.7%。
- 新增测试: 57 个, 文件:
  `tests/mutation/test_mutation_metrics.py`(35)、
  `tests/mutation/test_mutation_indicators.py`(9)、
  `tests/mutation/test_mutation_validation.py`(13)。
- 报告路径: `docs/mutation-report.md`(本文件)。
- 复现: `pytest tests/mutation -q` (57 passed) + `ruff check tests/mutation`(clean);
  变异判定逻辑见 TEMP harness(未进仓, 可按需重新生成)。
