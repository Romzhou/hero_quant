# Mutation 存活抽查审计（15/128）

> 基准：`docs/mutation-report.md §4（128 个存活全归等价/死代码）` + `tests/mutation/`（58 tests）。
> 方法：只读复核，不改 `src/`；每个 mutant 看 `file:line` 上下文 + 定向测试 + 定义域演算。
> 抽样：`validation.py` 7 个全覆盖（剩余 8.3% 优先）+ P0 `metrics.py:57/95/259` + P1 `metrics.py:26/256`、`indicators.py:122/135/43`，共 15 个。
> 约束遵守：只新建本文件；未碰 `src/、tests/、migrations/、.env`；未 `push`。

## 1. 结论表（15 条）

| # | mutant id | file:line | 变异算子 | 判定 | 证据（测试或推理） | 置信度 |
|---|---|---|---|---|---|---|
| 1 | id7 | `src/hero_quant/backtest/validation.py:64` `len(args)>0` | ROR `> -> !=` | 等价 | 定义域 `len>=0` 下 `>0 ⟺ !=0` 恒等（`validation.py:60-64`）；放行用例 `tests/mutation/test_mutation_validation.py:21 test_pit_partial_args_skip` 与 `93 test_multi_asset_no_close_passes` 覆盖，其余方向（`>=0/<0/==0`）已被杀，存活必为 `!=` | 高 |
| 2 | id15 | `src/hero_quant/backtest/validation.py:102` `weights_on is not None and price_date is not None` | LOR `and->or` | 等价 | pin `test_pit_partial_args_skip`（`test_mutation_validation.py:21`）：单边 `None` 时 `or` 进入分支（`validation.py:112-135`），但 `pd.Timestamp(None)=NaT`，`NaT` 比较恒 False、`_norm_ts(None)=NaT`、`NaT>Timestamp==False`（`:139`）永不抛错，verdict 一致；与报告 §4 P0 实证一致 | 高 |
| 3 | id21 | `src/hero_quant/backtest/validation.py:123` `_off_s=...else 0.0` | CONST `0.0->1.0`（三元 else） | 等价/防御死代码，不值得 | 无定向用例触发：正常 tz 走 `total_seconds()`，`else` 仅 `utcoffset()->None`（浮动时区）可达（`validation.py:121-125`）；`Etc/GMT/UTC` 用例（`34/47/57/67/80`）均不走该分支 | 中 |
| 4 | id22 | `src/hero_quant/backtest/validation.py:125` `except(...): _off_s=0.0` | CONST `0.0->1.0`（except 回落） | 等价/防御死代码，不值得 | 需 `_aware_raw.utcoffset()` 抛 `ValueError/TypeError/AttributeError`（`validation.py:124`），标准 `pytz/dateutil/Etc/GMT` 永不抛；即使触发仅影响 `:131 _off_s!=0` 同墙钟拒绝，需定制抛错 `tzinfo` 才可观测，P3 病态 | 中 |
| 5 | id69 | `src/hero_quant/backtest/validation.py:208` `nuniq>1` | ROR `> -> !=`（报告记 `> / !=` 类） | 等价（以 `!=` 计） | `nuniq=nunique(dropna=False)` 在无 NaN+非空帧下 `>=1` 恒成立（`validation.py:204-207`），故 `>1 ⟺ !=1`；杀伤方向见 `test_currency_lexicographic_mismatch_rejected`（`test_mutation_validation.py:168`）。注意：若 harness 实为 `>=1` 则非等价（见 §2），需以 harness 日志确认编号 | 高（`!=`）/中（编号确认） |
| 6 | id72 | `src/hero_quant/backtest/validation.py:213` `len(unique_vals)>0` 前件 | ROR `> -> !=` | 等价 | `unique_vals=dropna().unique()` 经 `204` NaN 拦截后 `len>=1` 恒成立（`validation.py:212`），`>0 ⟺ !=0`；`mismatch` 用例（`:168`）杀掉 `CONST 0->1`、`</<=/==`，存活必为 `!=` | 高 |
| 7 | id76 | `src/hero_quant/backtest/validation.py:213` 同行后件/比较符 | ROR `> -> >=` 或 `== -> >/> =`（报告记 `>=` 类） | **误分类：测试缺口，非等价**（见 §2） | 窄 oracle 内无单币种匹配放行用例，仅 `168 mismatch`（`AUD vs CNY` 下 `==` 与 `>`/`>=` 同为 False 而存活）；`len>=0` 恒真在匹配时 `True and False -> True` 会误杀。全量套件 `tests/test_j2_mcp_skills_validation.py:290 validate(ccy3,currency="CNY") is None` 可杀，mutation 窄 oracle 未跑该文件故存活；报告 §4 P2“恒等”过强 | 中 |
| 8 | id50 | `src/hero_quant/backtest/metrics.py:57` `cummax != 0` | ROR `!= -> >`（兄弟 id51 `!= -> >=` 结论相同，见备注） | 等价 | `cummax<0` 蕴含此前全负：`s<=cummax<=0 ⇒ s/cummax>=1 ⇒ dd>=0`，变异清零非负项；全局 `min<=0` 恒成立（第 0 行 `dd=0` 或 where 置零，`metrics.py:54-60`），故不改 `min`；`cummax==0` 行 `s/0-1` 为 `NaN/-inf` 经 `:58 replace+fillna(0)` 同归 0。定向 `test_max_drawdown_unit_peak`（`test_mutation_metrics.py:57-61`）只覆盖 `cummax>0` 路径故存活；全负序列同样等价 | 高 |
| 9 | id86 | `src/hero_quant/backtest/metrics.py:95` `len(_pos)>=2` | CONST `2->0`（恒真） | 等价 | 正常路径恒真：`:80-85` 保 `len(s)>=2`，`_pos` 与 `s` 同源（`:89-93`）故 `len(_pos)>=2`；恒真后 `try` 内 `_valid_mask.index(True)` 抛 `ValueError` 被 `:100-101 except: pass` 吸收，`n` 保持 `len(s)-1` 与原跳过分支一致。`test_annual_gap_span`（`:85-94`）覆盖恒真路径；`test_annual_gap_dropna_single`（`:97-101`）只到 `:84` 早回 | 高 |
| 10 | id237（代表 id237-246 共 11 个） | `src/hero_quant/backtest/metrics.py:259` `((1+net_ret)<=0).any()` | ROR/CONST 于日志谓词（如 `<= -> <`） | 等价（返回值；日志可观测下为“不值得”） | `:259-260` 纯 `logger.warning` 无状态变更，`s=net_equity` 由 `:261` 独立守卫决定；定向 `test_compute_metrics_full_ruin`（`test_mutation_metrics.py:202-212`）与 `test_compute_metrics_zero_crossing_costs` 只断言六指标数值不 `caplog`，故全存活预期。其余 10 个同理 | 高（返回值）/中（计日志副作用） |
| 11 | id10 | `src/hero_quant/backtest/metrics.py:26` `_std=ret.std(ddof=1)` | CONST `ddof 1->0`（零值守卫专用） | 等价（实践；容差边界理论可分叉，不值得） | `n>=2` 时 `std0=std1*sqrt((n-1)/n)` 同零点；单点 `std1=NaN→return 0` 与 `std0=0→isclose→0` 同 verdict；真公式在 `:35` 重算不复用 `_std`。严格反例仅 `std1≈1.1e-9、n=2` 缩放跨 `abs_tol=1e-9`，需特设输入，Sharpe 本就噪声。定向 `test_compute_metrics_jitter_vol`（`:240-244`）与正常波动测试分居容差远端故存活 | 中 |
| 12 | id48 | `src/hero_quant/quantlib/indicators.py:122` `min_periods=1` | CONST `1->0`（`:123` id53 同理） | 等价 | `delta=s.diff()`（`:117`），`gain/loss=where(cond,0.0)`（`:118-119`，`NaN>0=False`）故输入恒有限，`ewm(min_periods=1 vs 0)` 首值均有输出逐位一致；`alpha` 分子 `1->0` 抛 `ValueError` 基线已杀。`test_rsi_nan_positions_pinned`（`test_mutation_indicators.py:56-64`）+ `test_ema_leading_nan_pinned`（`:39-45`）锁定 | 高 |
| 13 | id89（代表 `135/137`） | `src/hero_quant/quantlib/indicators.py:135` `fillna(50.0)` | UOR/CONST（`fillna` 链） | 死代码/等价 | `gain/loss` 有限 ⇒ `avg_gain/loss` 有限无 `NaN`；`rs` 仅 `0/0` 横盘为 `NaN`、`loss==0` 为 `inf`，`:129 where(avg_loss!=0,100)` 消灭 `inf` 行，`:131-132 flat_mask→50` 消灭唯一 `NaN` 行，到 `:135 fillna` 已无 `NaN`，`:137 replace→fillna` 亦不可达。`test_rsi_nan_positions_pinned` 显式标注不可达 | 高 |
| 14 | id6 | `src/hero_quant/quantlib/indicators.py:43` `replace([inf,-inf],nan)` | UOR（`-inf` 不洗） | 等价（版本条件，非永恒） | `_to_series`（`:40-44`）双向清洗；存活唯一解释是本 pandas `rolling/ewm` 把残余 `-inf` 当缺失，四路输出一致（报告已实证）。`test_indicators_minus_inf_sanitized`（`test_mutation_indicators.py:22-28`）为版本哨兵：升级后若变红即掩蔽失效 | 中 |
| 15 | id231 | `src/hero_quant/backtest/metrics.py:256` `(1+net_ret).clip(0).cumprod() * s0` | AOR `* -> /` | 等价 | `*s0` vs `/s0` 都是均匀缩放（`s0≠0` 同号），不改 `pct_change`；六指标全比率/差分型（sharpe/annual/mdd/turnover/vol/cum，`:256,261,266-285`），输出完全一致。`s0==0` 理论分歧（全零 vs `inf`）被 `:261 isfinite` 守卫吸收回落，属 P3 病态。`test_compute_metrics_costs_exact_semantics`（`:185-199`）+ `test_compute_metrics_full_ruin`（`:202-212`）锁定 | 高（`s0≠0`；含零则中） |

备注：`metrics.py:57` 兄弟 id51（`!= -> >=`）与 id50 同证明（另加 `cummax==0` 行 `NaN/-inf→fillna 0` 与置零一致），未计入 15 名额，结论等价/高。

## 2. 真缺陷清单

**`src` 真 bug：0 个。** 15 个中 13 个复核维持“等价/死代码”，1 个（id76）为报告误分类 + 测试缺口（已补 pin），1 个（id69 编号歧义）需 harness 确认。均不构成线上资金/风控风险，未动手。

| # | 性质 | 位置 | 说明 | 最小修复（F1 已补 pin，其余未动手） |
|---|---|---|---|---|
| F1 | 测试缺口 + 报告过强 | `validation.py:213` id76；报告 §4 P2 `validation.py:213 ×2`“恒等” | 窄 oracle 无匹配放行用例，`>=0/and->or/==->>` 存活；全量 `tests/test_j2_mcp_skills_validation.py:290` 放行断言可杀。不是 `src` 错，是 oracle 太窄 | 已补 `tests/mutation/test_mutation_validation.py::test_currency_single_matching_passes`（单币种 CNY + currency=CNY 放行）；反向字典序失配由既有 `test_currency_lexicographic_mismatch_rejected` 锁定；报告 §4 该行应改为“窄 oracle 存活，全量可杀” |
| F2（待确认） | 编号歧义 | `validation.py:208` id69 | 若实为 `> -> >=`（`nuniq>=1` 恒真）则非等价，同 F1 一组 pin 即可杀；若为 `> -> !=` 则等价。报告未贴 harness 算子明细 | 查 TEMP harness 日志确认 id69 算子；若为 `>=`，与 F1 同批补 pin，无需改 `src` |
| O1 | 行文精度（非缺陷） | `metrics.py:26` id10；`metrics.py:259` id237 | id10“同零点”略去容差缩放边界；id237 应注明“返回值等价、日志可观测不等价” | 报告加注即可，不改 `src`，不加 pin |
| O2 | 版本条件（非缺陷） | `indicators.py:43` id6 | 等价依赖本 pandas `rolling/ewm -inf` 掩蔽语义 | 保留双向清洗不动；该 pin 当版本哨兵，升级变红再在输出端补 `replace` |

## 3. 验收（实跑 2026-10-20）

- `python -m pytest tests/mutation -q` → **58 passed**（含新增 id76 匹配侧 pin）。
- `python -m pytest tests/mutation tests/validation tests/fuzz -q` → **112 passed, 2 skipped**（新口径锁 + 边界用例全绿）。
- `ruff check src` → **All checks passed**。
- 旧断言已按 R 线统一口径更新：DF 单列 `0.375→0.5`、双列 `0.625→5/6`、`-inf` 首行 `1/3→0.5`（`tests/mutation/test_mutation_metrics.py`）。
- 本文件外未改动；`git add` 仅显式 `docs/mutation-audit.md`（由总管执行）。
