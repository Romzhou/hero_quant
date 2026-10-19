# tests/mutation/test_mutation_indicators.py
"""N线变异试点: quantlib/indicators.py 高价值存活 mutant 的定向杀伤测试.

只新增本目录文件, 不改现有测试与 src. 每个用例注释标出所杀 mutant.
约定: 纯 pandas/numpy 确定性断言, 无外部依赖.
"""

import math

import pandas as pd
import pytest


def test_to_series_dataframe_single_col():
    """杀 indicators.py:25 [ROR/CONST] shape/polit: 单列 DataFrame 必须取首列, 不得回落空."""
    from hero_quant.quantlib.indicators import sma

    out = sma(pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0]}), window=2)
    assert out.isna().iloc[0] and out.iloc[-1] == pytest.approx(3.5)


def test_indicators_minus_inf_sanitized():
    """钉 indicators.py:43 [UOR] -inf 清洗 (已证等价: pandas rolling/ewm 把 ±inf 当缺失, 输出一致)."""
    from hero_quant.quantlib.indicators import sma

    out = sma(pd.Series([float("-inf"), 1.0, 2.0, 3.0, 4.0]), window=3)
    assert out.isna().iloc[2  # mutant 在此 pandas 版本下输出一致, 故为回归钉
    ]


def test_sma_window_one_identity():
    """杀 indicators.py:57 [CONST] w <= 0 -> w <= 1 : 窗口 1 是恒等变换, 不得抛错."""
    from hero_quant.quantlib.indicators import sma

    s = pd.Series([3.0, 1.0, 2.0])
    assert sma(s, window=1).tolist() == [3.0, 1.0, 2.0]


def test_ema_leading_nan_pinned():
    """钉 indicators.py:95 min_periods=1 -> 0 (已证等价): 前导 NaN 行为锁定, 供回归."""
    from hero_quant.quantlib.indicators import ema

    out = ema(pd.Series([float("nan"), 1.0, 2.0, 3.0]), span=2)
    assert out.isna().iloc[0]
    assert out.iloc[1:].tolist() == pytest.approx([1.0, 5.0 / 3.0, 23.0 / 9.0])


def test_rsi_small_moves_threshold():
    """杀 indicators.py:119 [CONST] delta < 0 -> delta < 1 : 亚 1 点波动不得被计为下跌."""
    from hero_quant.quantlib.indicators import rsi

    s = pd.Series([0.5 * i for i in range(15)])
    assert rsi(s, period=14).iloc[-1] == pytest.approx(100.0)  # mutant 给出 50.0


def test_rsi_nan_positions_pinned():
    """钉 indicators.py:135/137 fillna(50.0) (已证不可达): 内部 NaN 经 where 置零, 不走 fillna."""
    from hero_quant.quantlib.indicators import rsi

    s = pd.Series(
        [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, float("nan"), 15, 16, 17, 18, 19, 20.0]
    )
    out = rsi(s, period=14)
    assert out.iloc[13] == pytest.approx(100.0) and out.iloc[-1] == pytest.approx(100.0)


def test_bollinger_num_std_fallback():
    """杀 indicators.py:160/164/165 非法倍数回落: 不可解析与负倍数必须回落 k=2.0, 不得反转带宽."""
    from hero_quant.quantlib.indicators import bollinger

    s = pd.Series(range(1, 21), dtype=float)
    _, up_bad, _ = bollinger(s, window=5, num_std="bad")
    _, up_neg, lo_neg = bollinger(s, window=5, num_std=-1)
    assert up_bad.iloc[-1] == pytest.approx(18.0 + 2.0 * math.sqrt(2.5))
    # 负倍数若直接使用会导致 upper < lower; 回落后必须与 k=2 一致
    assert up_neg.iloc[-1] == pytest.approx(up_bad.iloc[-1])
    assert (up_neg.dropna() > lo_neg.dropna()).all()
    # 自定义正倍数必须被尊重 (杀 :164 [ROR] k < 0 -> k != 0: mutant 会把 k=5 回落成 2.0)
    _, up5, _ = bollinger(s, window=5, num_std=5)
    assert up5.iloc[-1] == pytest.approx(18.0 + 5.0 * math.sqrt(2.5))


def test_macd_window_alias():
    """杀 indicators.py:194 [ROR] fast == 12 全家: window 别名仅在 fast 取默认时生效."""
    from hero_quant.quantlib.indicators import macd

    s = pd.Series(range(1, 31), dtype=float)
    aliased = macd(s, window=20)
    explicit = macd(s, fast=20)
    for x, y in zip(aliased, explicit):
        assert (x - y).abs().max() == 0
    ignored = macd(s, fast=15, window=20)
    plain = macd(s, fast=15)
    for x, y in zip(ignored, plain):
        assert (x - y).abs().max() == 0
    # 低 fast 值 + window 别名必须忽略别名 (杀 :194 [ROR] fast == 12 -> fast <= 12)
    low = macd(s, fast=5, window=20)
    low_plain = macd(s, fast=5)
    for x, y in zip(low, low_plain):
        assert (x - y).abs().max() == 0


def test_indicators_max_drawdown_basic():
    """indicators.max_drawdown 回归钉: 基本语义与 metrics 版一致."""
    from hero_quant.quantlib.indicators import max_drawdown

    assert max_drawdown([100, 150, 75, 150.0]) == pytest.approx(-0.5)
    assert max_drawdown([100, 101, 102.0]) == 0.0
    assert max_drawdown([]) == 0.0
