"""Round7 X2b — `backtest.metrics.turnover` docstring 示例与实现真值对齐。

背景：docstring 原有两例自相矛盾——先说 `w=[0,1,0]` 单边换手「应为 1.0」，
后说 `w=[0,1,0,1,0]` 按日均口径「得 0.5」，未说明「1.0」是区间单边总额、
不是函数返回值。Round7 订正 docstring，用实跑真值区分两个口径：

    本函数返回 = sum(|Δw|) / 2 / (n-1)   （日均单边）
    区间单边总额 = sum(|Δw|) / 2          （不除天数）

    w=[0,1,0]     : sum(|Δw|)=2 → 单边总额 1.0 → 日均 1.0/(3-1) = 0.5
    w=[0,1,0,1,0] : sum(|Δw|)=4 → 单边总额 2.0 → 日均 2.0/(5-1) = 0.5

已有 pin（不重复造轮子）：`turnover(weights=[0,1,0,1,0]) == 0.5` 已被
tests/test_j4a_backtest_quantlib.py:72、tests/test_retest271_lane_d2.py:187、
tests/test_w4_backtest_metrics_branches.py:151 锁定；`[0,1,0]` 这一例此前
**无人 pin**，故本文件补上，并额外把 docstring 的「区间单边总额 / (n-1)」
换算关系本身钉成用例，保证文档公式 = 实现。
"""

import pytest

from hero_quant.backtest.metrics import turnover

# 文档示例 1：w=[0,1,0] —— sum(|Δw|)=2，区间单边总额 1.0，日均 1.0/(3-1)=0.5
# 此前无任何用例 pin 该权重的最短序列，属新增锁。
W_THREE_STEPS = [0, 1, 0]
# 文档示例 2：w=[0,1,0,1,0] —— sum(|Δw|)=4，区间单边总额 2.0，日均 2.0/(5-1)=0.5
W_FIVE_STEPS = [0, 1, 0, 1, 0]


def test_turnover_weights_three_steps_returns_half():
    """docstring 例 1 的真值：turnover(w=[0,1,0]) 返回 0.5（不是 1.0）。

    「1.0」是区间单边总额口径；本函数按日均口径返回 0.5。
    """
    assert turnover(weights=W_THREE_STEPS) == pytest.approx(0.5)


def test_turnover_weights_docstring_two_tier_formula_holds():
    """docstring 两条口径的一致性：日均 = 区间单边总额 / (n-1)，两例都成立。

    区间单边总额由权重序列直接算出（sum(|Δw|)/2），再除以差分个数 (n-1)，
    必须等于函数返回值 —— 一旦实现改成别的日均方式（如 /n）本用例即红。
    """
    for weights in (W_THREE_STEPS, W_FIVE_STEPS):
        side_total = sum(abs(b - a) for a, b in zip(weights, weights[1:])) / 2
        expected = side_total / (len(weights) - 1)
        assert turnover(weights=weights) == pytest.approx(expected)


def test_turnover_weights_three_steps_side_total_is_one():
    """docstring 里「1.0」的含义锁定：w=[0,1,0] 的区间单边总额是 1.0。

    这是文档表述的锚点：1.0 属区间单边总额口径，不等于本函数返回值。
    """
    weights = W_THREE_STEPS
    side_total = sum(abs(b - a) for a, b in zip(weights, weights[1:])) / 2
    assert side_total == pytest.approx(1.0)
