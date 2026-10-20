"""R线：turnover DF↔Series↔weights 同输入同输出对照锁。

约定（见 docs/quant-semantics.md §2）：sum(|Δ|)/2/(n_valid-1)，首日剔除。
DF 路径须用 sum(min_count=1) 使首行 NaN 被 dropna 剔除，与 Series/weights 一致。
旧稀释值 0.375/0.625/1/3 为 bug，不得回退。
"""

import pandas as pd
import pytest

from hero_quant.backtest.metrics import turnover


def test_single_col_df_series_weights_parity():
    vals = [1.0, 2.0, 3.0, 2.0]
    df_v = turnover(positions=pd.DataFrame({"a": vals}))
    s_v = turnover(positions=pd.Series(vals))
    w_v = turnover(weights=vals)
    assert df_v == pytest.approx(0.5)  # 稀释 bug 下为 0.375
    assert s_v == pytest.approx(0.5)
    assert w_v == pytest.approx(0.5)
    assert df_v == pytest.approx(s_v) == pytest.approx(w_v)


def test_multicol_manual_value():
    df2 = pd.DataFrame({"a": [1.0, 2.0, 3.0, 2.0], "b": [10.0, 10.0, 12.0, 12.0]})
    # 行和 diff: [NaN, 1, 3, 1] -> 5/2/3
    assert turnover(positions=df2) == pytest.approx(5 / 2 / 3)


def test_minus_inf_leading_parity():
    vals = [float("-inf"), 1.0, 2.0, 3.0]
    df_v = turnover(positions=pd.DataFrame({"a": vals}))
    s_v = turnover(positions=pd.Series(vals))
    assert s_v == pytest.approx(0.5)
    assert df_v == pytest.approx(0.5)  # 稀释 bug 下为 1/3


def test_middle_gap_bridges_parity():
    s_vals = pd.Series([1.0, float("nan"), 3.0])
    d_vals = pd.DataFrame({"a": [1.0, float("nan"), 3.0]})
    # dropna 后 [1,3] -> |Δ|=2 -> 2/2/1 = 1.0，两路径一致
    assert turnover(positions=s_vals) == pytest.approx(1.0)
    assert turnover(positions=d_vals) == pytest.approx(1.0)


def test_stable_and_single_point_zero():
    assert turnover(positions=pd.Series([100.0] * 5)) == 0.0
    assert turnover(positions=pd.DataFrame({"a": [100.0] * 5})) == 0.0
    assert turnover(weights=[0.5, 0.5]) == 0.0
    assert turnover(positions=pd.Series([5.0])) == 0.0
    assert turnover(positions=pd.DataFrame({"a": [5.0]})) == 0.0


def test_weights_series_small_parity():
    assert turnover(weights=[0, 1, 0]) == pytest.approx(0.5)
    assert turnover(positions=pd.Series([0.0, 1.0, 0.0])) == pytest.approx(0.5)
    assert turnover(positions=pd.DataFrame({"a": [0.0, 1.0, 0.0]})) == pytest.approx(0.5)
