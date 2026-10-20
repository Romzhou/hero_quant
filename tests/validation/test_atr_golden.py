"""U2：ATR 手算 golden 回归锁。

不依赖 pandas-ta / TA-Lib（CI 无此包），期望值全部手算推导。
ATR 口径：TR=max(H-L, |H-prevC|, |L-prevC|)，TR[0]=H-L；
ATR[n-1]=mean(TR[0:n])，之后 ATR[i]=(ATR[i-1]*(n-1)+TR[i])/n（Wilder/TA-Lib 一致）。
"""

import math

import numpy as np
import pandas as pd
import pytest

from hero_quant.quantlib import indicators as Q


def _ohlc_5bar():
    high = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0])
    low = pd.Series([9.0, 9.5, 10.0, 9.5, 11.0])
    close = pd.Series([9.5, 10.5, 11.0, 10.5, 12.0])
    return high, low, close


def test_atr_hand_golden_period3():
    """5 bar 手算：TR=[1.0,1.5,2.0,1.5,2.5]，n=3 → [nan,nan,1.5,1.5,5.5/3]。"""
    h, lo, c = _ohlc_5bar()
    out = Q.atr(h, lo, c, 3)
    assert len(out) == 5
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(1.5)
    assert out.iloc[3] == pytest.approx(1.5)
    assert out.iloc[4] == pytest.approx(5.5 / 3.0)


def test_atr_tr_golden_values():
    """TR 中间值锁定：bar1 TR=1.5（|11-9.5|）、bar4 TR=2.5（|13-10.5|）。"""
    h, lo, c = _ohlc_5bar()
    # period=1 时 ATR==TR，直接锁定 TR 全序列
    out = Q.atr(h, lo, c, 1)
    assert out.tolist() == pytest.approx([1.0, 1.5, 2.0, 1.5, 2.5])


def test_atr_dataframe_input_parity():
    """DataFrame(high/low/close) 与三序列分参结果一致。"""
    h, lo, c = _ohlc_5bar()
    df = pd.DataFrame({"high": h, "low": lo, "close": c})
    a = Q.atr(df, period=3)
    b = Q.atr(h, lo, c, period=3)
    pd.testing.assert_series_equal(a, b, check_names=False)


def test_atr_wilder_loop_crosscheck_random200():
    """200 bar 独立 Wilder 实现交叉：maxabs 必须为 0。"""
    rng = np.random.default_rng(11)
    base = 100 + np.cumsum(rng.normal(0.0, 1.0, size=200))
    spread = np.abs(rng.normal(1.0, 0.3, size=200)) + 0.2
    high = pd.Series(base + spread / 2)
    low = pd.Series(base - spread / 2)
    close = pd.Series(base + rng.normal(0.0, 0.2, size=200))
    n = 14
    got = Q.atr(high, low, close, n)

    # 独立实现：pandas 算 TR + 纯 Python Wilder 递推
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = float(high.iloc[0] - low.iloc[0])
    exp = pd.Series([np.nan] * len(close), dtype=float)
    vals = tr.tolist()
    prev = float(sum(vals[:n]) / n)
    exp.iloc[n - 1] = prev
    for i in range(n, len(vals)):
        prev = (prev * (n - 1) + vals[i]) / n
        exp.iloc[i] = prev
    diff = (got - exp).abs()
    assert float(diff.iloc[n - 1 :].max()) == 0.0


def test_atr_insufficient_bars_all_nan():
    """bars < period 时全 NaN（诚实置空，不填 0 冒充）。"""
    h, lo, c = _ohlc_5bar()
    out = Q.atr(h.iloc[:2], lo.iloc[:2], c.iloc[:2], 3)
    assert len(out) == 2
    assert bool(out.isna().all())


def test_atr_empty():
    h = pd.Series(dtype=float)
    out = Q.atr(h, pd.Series(dtype=float), pd.Series(dtype=float), 14)
    assert len(out) == 0


def test_atr_invalid_window_raises():
    h, lo, c = _ohlc_5bar()
    for bad in (0, -3, "x", None):
        with pytest.raises((ValueError, TypeError)):
            Q.atr(h, lo, c, bad)


def test_atr_length_mismatch_raises():
    h, lo, c = _ohlc_5bar()
    with pytest.raises(ValueError, match="length mismatch"):
        Q.atr(h.iloc[:4], lo, c, 3)


def test_atr_nan_gap_honest_no_fake():
    """中间 NaN 的 TR 对应 ATR 为 NaN 且不污染后续 Wilder 状态。"""
    h, lo, c = _ohlc_5bar()
    h2 = h.copy()
    h2.iloc[3] = np.nan
    out = Q.atr(h2, lo, c, 3)
    # seed ATR[2]=1.5 不变；bar3 NaN；bar4 用 ATR[2] 直接递推：(1.5*2+2.5)/3
    assert out.iloc[2] == pytest.approx(1.5)
    assert math.isnan(out.iloc[3])
    assert out.iloc[4] == pytest.approx((1.5 * 2 + 2.5) / 3)


def test_atr_inf_treated_as_nan():
    h, lo, c = _ohlc_5bar()
    h2 = h.copy()
    h2.iloc[1] = np.inf
    out = Q.atr(h2, lo, c, 3)
    # TR[1] 因 Inf→NaN；有效 TR 仅 4 个仍 ≥3，种子顺延：mean(1.0,2.0,1.5)=1.5 落在 index3
    assert math.isnan(out.iloc[2])
    assert out.iloc[3] == pytest.approx((1.0 + 2.0 + 1.5) / 3)
    assert out.iloc[4] == pytest.approx((1.5 * 2 + 2.5) / 3)


def test_atr_alias_and_package_export():
    """别名 n/window/span 等价 + quantlib 包导出 atr。"""
    h, lo, c = _ohlc_5bar()
    base = Q.atr(h, lo, c, period=3)
    for kw in ({"n": 3}, {"window": 3}, {"span": 3}):
        pd.testing.assert_series_equal(Q.atr(h, lo, c, **kw), base, check_names=False)
    import hero_quant.quantlib as qlib

    assert "atr" in qlib.__all__
    assert qlib.atr is Q.atr
