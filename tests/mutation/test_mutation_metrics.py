# tests/mutation/test_mutation_metrics.py
"""N线变异试点: backtest/metrics.py 高价值存活 mutant 的定向杀伤测试.

只新增本目录文件, 不改现有测试与 src. 每个用例注释标出所杀 mutant
(变异体 id 体系见 docs/mutation-report.md; 形如 metrics.py:<行> [<类>] <描述>).
约定: 纯 pandas/numpy 确定性断言, 无外部依赖.
"""

import math

import numpy as np
import pandas as pd
import pytest


def pd_series_up():
    return pd.Series(100.0 * (1.002 ** np.arange(60)))


def test_sharpe_risk_free_exact():
    """杀 metrics.py:33 [AOR] rf_daily = risk_free / periods -> * : 无风险折日必须除法年化."""
    from hero_quant.backtest.metrics import sharpe_ratio

    eq = pd.Series([100.0, 101.0] * 30)
    a = sharpe_ratio(eq)
    b = sharpe_ratio(eq, risk_free=0.05)
    assert a > 0 and b < a
    # 精确值: 除法口径 (mutant * 口径 rf_daily=12.6 会给出约 -1e4 量级)
    ret = eq.pct_change().dropna()
    exp_b = (ret - 0.05 / 252).mean() / ret.std(ddof=1) * math.sqrt(252)
    assert b == pytest.approx(float(exp_b), rel=1e-9)
    assert -50.0 < b < a


def test_sharpe_all_nan():
    """杀 metrics.py:24 [CONST] ret.empty -> return 1.0 : 全 NaN 权益必须回落 0."""
    from hero_quant.backtest.metrics import sharpe_ratio

    assert sharpe_ratio(pd.Series([float("nan")] * 3)) == 0.0


def test_sharpe_minus_inf_sanitized():
    """杀 metrics.py:22 [UOR] -inf -> +inf : 跌破零点的 -inf 收益必须被清洗, 不得泄漏进 std."""
    from hero_quant.backtest.metrics import sharpe_ratio

    sr = sharpe_ratio(pd.Series([0.0, -50.0, -40.0, -30.0]))
    assert math.isfinite(sr) and sr < -10.0  # 清洗后约为 -101; mutant 走 guard 直接给 0.0


def test_max_drawdown_none():
    """杀 metrics.py:43 [LOR] or -> and : None 输入走 or 短路回落 0, 不得崩溃."""
    from hero_quant.backtest.metrics import max_drawdown

    assert max_drawdown(None) == 0.0


def test_max_drawdown_unit_peak():
    """杀 metrics.py:57 [CONST] cummax != 0 -> != 1 : 单位峰值行的回撤不得被清零."""
    from hero_quant.backtest.metrics import max_drawdown

    assert max_drawdown(pd.Series([1.0, 0.5])) == pytest.approx(-0.5)


def test_max_drawdown_all_nan():
    """杀 metrics.py:58 [CONST] fillna(0.0) -> 1.0 : 全 NaN 回撤必须回落 0, 不得变 1."""
    from hero_quant.backtest.metrics import max_drawdown

    assert max_drawdown(pd.Series([float("nan"), float("nan")])) == 0.0


def test_max_drawdown_minus_inf():
    """杀 metrics.py:58 [UOR] -inf -> +inf : -inf 回撤必须被清洗, 保留真实 -0.5."""
    from hero_quant.backtest.metrics import max_drawdown

    assert max_drawdown(pd.Series([100.0, 50.0, float("-inf")])) == pytest.approx(-0.5)


def test_annual_two_point():
    """杀 metrics.py:70/104/114 两点 CAGR: n=1 按单期跨度年化, 不得回落 0."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([100.0, 200.0])) == pytest.approx(2.0**252 - 1)


def test_annual_gap_span():
    """杀 metrics.py:95/98/99 缺口跨度: 年化基数取原序列有效跨度, 不用 dropna 后长度."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([100.0, float("nan"), float("nan"), 200.0])) == pytest.approx(
        2.0**84 - 1
    )
    assert annual_return(pd.Series([100.0, float("nan"), 200.0, 300.0])) == pytest.approx(
        3.0**84 - 1
    )


def test_annual_gap_dropna_single():
    """杀 metrics.py:84/85 缺口去重后单点: 必须回落 0, 不得计算."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([100.0, float("nan")])) == 0.0


def test_annual_bankrupt_end():
    """杀 metrics.py:119 [ROR] ratio <= 0 -> < : 终值为零的破产序列必须回落 0, 不得算出 -1."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([100.0, 0.0])) == 0.0


def test_annual_declining():
    """杀 metrics.py:119 [CONST] ratio <= 1 : 亏损序列年化必须为负 (0.5^252-1 == -1.0)."""
    from hero_quant.backtest.metrics import annual_return

    ar = annual_return(pd.Series([200.0, 100.0]))
    assert ar == pytest.approx(-1.0) and ar < 0.0


def test_annual_dust_start():
    """杀 metrics.py:111/284 dust 容差: 尘埃起点 (5e-13) 必须回落 0, 不得爆炸成年化垃圾."""
    from hero_quant.backtest.metrics import annual_return, compute_metrics

    assert annual_return(pd.Series([5e-13, 1.0])) == 0.0
    assert compute_metrics(pd.Series([5e-13, 1.0]))["cumulative_return"] == 0.0


def test_annual_overflow():
    """杀 metrics.py:127/129 溢出守卫: ratio**n 上溢 inf 必须回落 0, 不得泄漏 1."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([1.0, 1e308])) == 0.0


def test_turnover_dataframe_paths():
    """锁 metrics.py:156-160 DataFrame 换手分支: 与 Series 同口径 (/2 单边, 首日剔除)."""
    from hero_quant.backtest.metrics import turnover

    # 单列: diff 和 3, 分母 n-1=3 -> 3/2/3 = 0.5 (与 Series 路径一致)
    assert turnover(positions=pd.DataFrame({"a": [1.0, 2.0, 3.0, 2.0]})) == pytest.approx(
        0.5
    )
    # 双列: diff 和 3+2=5, 分母 n-1=3 -> 5/2/3 = 5/6
    df2 = pd.DataFrame({"a": [1.0, 2.0, 3.0, 2.0], "b": [10.0, 10.0, 12.0, 12.0]})
    assert turnover(positions=df2) == pytest.approx(5.0 / 6.0)


def test_turnover_weights_edge():
    """杀 metrics.py:178/183 单点与恒定权重: 必须为 0, 不得计算出换手."""
    from hero_quant.backtest.metrics import turnover

    assert turnover(weights=[0.5]) == 0.0
    assert turnover(weights=[1.0, 1.0, 1.0]) == 0.0


def test_turnover_weights_nonfinite_fail_closed():
    """杀 metrics.py:158/176/177 脏权重守卫: 含 inf/NaN 的权重必须回落 0, 不得清洗后计算."""
    from hero_quant.backtest.metrics import turnover

    assert turnover(weights=[0.0, float("inf"), 1.0]) == 0.0
    assert turnover(weights=[0.0, float("nan"), 1.0]) == 0.0


def test_compute_metrics_empty_full_dict():
    """杀 metrics.py:230-234 空序列零字典: 六个指标必须全 0."""
    from hero_quant.backtest.metrics import compute_metrics

    assert compute_metrics(pd.Series(dtype=float)) == {
        "sharpe": 0.0,
        "annual_return": 0.0,
        "max_drawdown": 0.0,
        "turnover": 0.0,
        "volatility": 0.0,
        "cumulative_return": 0.0,
    }


def test_compute_metrics_costs_none_and_garbage():
    """杀 metrics.py:242/244 costs 契约: None 与不可解析 costs 必须等价于零成本."""
    from hero_quant.backtest.metrics import compute_metrics

    eq = pd_series_up()
    assert compute_metrics(eq, costs=None) == compute_metrics(eq, costs=0.0)
    assert compute_metrics(eq, costs="bad") == compute_metrics(eq, costs=0.0)


def test_compute_metrics_costs_exact_semantics():
    """杀 metrics.py:248/250/256 additive-drag 接线: 成本是每 Bar 固定扣除 (首 Bar 不扣),
    不是乘性缩放; 精确重建净权益并断言."""
    from hero_quant.backtest.metrics import compute_metrics

    eq = pd_series_up()
    got = compute_metrics(eq, costs=0.001)
    gross = eq.pct_change().fillna(0.0).replace([np.inf, -np.inf], 0.0)
    net = gross - 0.001
    net.iloc[0] = float(gross.iloc[0])
    rebuilt = (1 + net).clip(lower=0.0).cumprod() * float(eq.iloc[0])
    assert got["cumulative_return"] == pytest.approx(float(rebuilt.iloc[-1] / rebuilt.iloc[0] - 1))
    free = compute_metrics(eq)
    assert got["annual_return"] < free["annual_return"]
    assert got["cumulative_return"] < free["cumulative_return"]


def test_compute_metrics_full_ruin():
    """杀 metrics.py:261 破产吸收: 巨额成本下净权益逐 Bar 截断为 0, 指标全零仅回撤/累计为 -1."""
    from hero_quant.backtest.metrics import compute_metrics

    got = compute_metrics(pd_series_up(), costs=5.0)
    assert got["sharpe"] == 0.0
    assert got["annual_return"] == 0.0
    assert got["max_drawdown"] == pytest.approx(-1.0)
    assert got["turnover"] == 0.0
    assert got["volatility"] == 0.0
    assert got["cumulative_return"] == pytest.approx(-1.0)


def test_compute_metrics_zero_crossing_costs():
    """杀 metrics.py:247 costs+清零穿越: +inf 清洗缺失会导致成本路径整体旁路 (cost == free)."""
    from hero_quant.backtest.metrics import compute_metrics

    eq = pd.Series([100.0, 0.0, 50.0, 51.0, 52.0])
    free = compute_metrics(eq)
    cost = compute_metrics(eq, costs=0.01)
    assert math.isfinite(cost["cumulative_return"])
    assert cost["cumulative_return"] < free["cumulative_return"]


def test_compute_metrics_unit_scale():
    """杀 metrics.py:284 [CONST] isclose(start, 0) -> isclose(start, 1): 单位净值起点必须正常计算."""
    from hero_quant.backtest.metrics import compute_metrics

    assert compute_metrics(pd.Series([1.0, 1.1]))["cumulative_return"] == pytest.approx(0.1)


def test_compute_metrics_inf_start():
    """杀 metrics.py:284 [LOR] and -> or : inf 起点累计收益必须回落 0, 不得算出 -1."""
    from hero_quant.backtest.metrics import compute_metrics

    assert compute_metrics(pd.Series([float("inf"), 100.0]))["cumulative_return"] == 0.0


def test_compute_metrics_jitter_vol():
    """杀 metrics.py:276 容差零判断: 1e-13 抖动波动率必须为 0."""
    from hero_quant.backtest.metrics import compute_metrics

    assert compute_metrics(pd.Series(100.0 + 1e-13 * np.arange(60.0)))["volatility"] == 0.0


def test_compute_metrics_single_point():
    """杀 metrics.py:276/281/292 单点序列: 波动率与累计收益必须为 0."""
    from hero_quant.backtest.metrics import compute_metrics

    got = compute_metrics(pd.Series([100.0]))
    assert got["volatility"] == 0.0
    assert got["cumulative_return"] == 0.0


def test_compute_metrics_minus_inf_vol():
    """杀 metrics.py:273/277 -inf 清洗: 跌破零点的 -inf 不得泄漏进波动率/Sharpe."""
    from hero_quant.backtest.metrics import compute_metrics

    got = compute_metrics(pd.Series([0.0, -50.0, -40.0, -30.0]))
    assert got["volatility"] > 0.1 and math.isfinite(got["volatility"])
    assert got["sharpe"] < -10.0


def test_annual_trailing_gap():
    """杀 metrics.py:98 [UOR 切片步长]/[AOR 第二个减号变加]: 尾部缺口不得拉长年化基数."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([100.0, 200.0, float("nan")])) == pytest.approx(2.0**252 - 1)


def test_annual_leading_gap():
    """杀 metrics.py:99 [AOR] _i1 - _i0 -> _i1 + _i0 : 头部缺口跨度必须按有效区间, 不得膨胀."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([float("nan"), 100.0, 200.0])) == pytest.approx(2.0**252 - 1)


def test_annual_dust_pair():
    """杀 metrics.py:111 [CONST] 1e-12 -> 0.0 : 尘埃起点对 (ratio 有限) 必须回落 0, 不得算出天文年化."""
    from hero_quant.backtest.metrics import annual_return

    assert annual_return(pd.Series([1e-13, 2e-13])) == 0.0


def test_turnover_minus_inf_paths():
    """杀 metrics.py:156/162 [UOR] -inf 清洗: 持仓 -inf 必须清洗后计算."""
    from hero_quant.backtest.metrics import turnover

    # DataFrame 路径: 首行 -inf 清洗后剔除, diff 和 3, 分母 n-1=3 -> 3/2/3 = 0.5
    assert turnover(positions=pd.DataFrame({"a": [float("-inf"), 1.0, 2.0, 3.0]})) == pytest.approx(0.5)
    # Series 路径: [nan,1,1] -> 2/2/2
    assert turnover(positions=pd.Series([float("-inf"), 1.0, 2.0, 3.0])) == pytest.approx(0.5)


def test_turnover_positions_single():
    """钉 metrics.py:169 (已证为 except 回落, 不可达): 单点持仓走末端回落, 锁定为 0."""
    from hero_quant.backtest.metrics import turnover

    assert turnover(positions=pd.Series([5.0])) == 0.0


def test_compute_metrics_unit_costs():
    """杀 metrics.py:245 [CONST] isclose(costs, 0) -> isclose(costs, 1): 1.0/Bar 成本是灭顶之灾, 不得按零成本."""
    from hero_quant.backtest.metrics import compute_metrics

    got = compute_metrics(pd_series_up(), costs=1.0)
    assert got["cumulative_return"] == pytest.approx(-1.0)


def test_compute_metrics_two_point_costs():
    """杀 metrics.py:245 [ROR] len >= 2 -> > / != : 两点序列也要走成本分支, 首 Bar 免扣."""
    from hero_quant.backtest.metrics import compute_metrics

    got = compute_metrics(pd.Series([100.0, 110.0]), costs=0.01)
    assert got["cumulative_return"] == pytest.approx(0.09)


def test_compute_metrics_cum_overflow():
    """杀 metrics.py:287 非有限累计守卫: ratio 上溢 inf 必须回落 0."""
    from hero_quant.backtest.metrics import compute_metrics

    got = compute_metrics(pd.Series([1.1e-12, 1e308]))
    assert got["annual_return"] == 0.0
    assert got["cumulative_return"] == 0.0
