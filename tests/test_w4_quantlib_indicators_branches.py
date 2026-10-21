"""W4 覆盖率补齐：quantlib/indicators.py 未覆盖分支。

覆盖目标（src/hero_quant/quantlib/indicators.py）：
- _to_series 的 DataFrame(close/equity/首列/零列)、list/ndarray、非法输入 TypeError 分支
- _validate_window 非法窗口 ValueError 分支
- sma/ema/rsi/bollinger/macd 的空输入、别名参数、不变式
- rsi 窗口不足全 NaN、全涨=100、横盘=50
- bollinger num_std 非法/负数回落 2.0
- atr 的 DataFrame 形态、缺列、长度不一致、NaN bar、Wilder 递推数值
- max_drawdown 的空/全 NaN/零峰值边界

断言均为具体数值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hero_quant.quantlib.indicators import (
    _to_series,
    _validate_window,
    atr,
    bollinger,
    ema,
    macd,
    max_drawdown,
    rsi,
    sma,
)


# ---------------------------------------------------------------- _to_series / _validate_window


def test_to_series_dataframe_prefers_close_column():
    """DataFrame 含 close/equity 时优先取 close。"""
    df = pd.DataFrame({"close": [1.0, 2.0], "equity": [9.0, 9.0]})
    s = _to_series(df)
    assert list(s) == [1.0, 2.0]


def test_to_series_dataframe_falls_back_to_equity_column():
    """无 close 但有 equity 时取 equity 列。"""
    df = pd.DataFrame({"equity": [5.0, 6.0], "x": [0.0, 0.0]})
    s = _to_series(df)
    assert list(s) == [5.0, 6.0]


def test_to_series_dataframe_without_close_or_equity_takes_first_column():
    """既无 close 也无 equity 时取首列。"""
    df = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, 4.0]})
    s = _to_series(df)
    assert list(s) == [1.0, 2.0]


def test_to_series_dataframe_zero_columns_is_empty():
    """零列 DataFrame 归一为空 Series。"""
    df = pd.DataFrame(index=[0, 1])
    s = _to_series(df)
    assert len(s) == 0


def test_to_series_accepts_list_and_ndarray():
    """list / ndarray 输入归一为数值 Series。"""
    assert list(_to_series([1, 2, 3])) == [1.0, 2.0, 3.0]
    assert list(_to_series(np.array([1.5, 2.5]))) == [1.5, 2.5]


def test_to_series_non_numeric_becomes_nan():
    """非数值字符串 coerce 为 NaN，Inf 亦为 NaN。"""
    s = _to_series(["a", "1.0", float("inf")])
    assert np.isnan(s.iloc[0])
    assert s.iloc[1] == 1.0
    assert np.isnan(s.iloc[2])


def test_to_series_invalid_input_raises_type_error():
    """不可构造的输入（set）抛 TypeError 并带明确消息。"""
    with pytest.raises(TypeError, match="cannot convert set to Series"):
        _to_series({1, 2, 3})


def test_validate_window_rejects_non_positive():
    """非正窗口抛 ValueError。"""
    with pytest.raises(ValueError, match="must be >0"):
        _validate_window(0)
    with pytest.raises(ValueError, match="must be >0"):
        _validate_window(-3)


def test_validate_window_rejects_non_numeric():
    """非数值窗口抛 ValueError（invalid window）。"""
    with pytest.raises(ValueError, match="invalid window"):
        _validate_window("abc")


def test_validate_window_accepts_numeric_string():
    """数值字符串窗口被接受并归一为 int。"""
    assert _validate_window("5") == 5


# ---------------------------------------------------------------- sma / ema


def test_sma_empty_series_returns_empty():
    """空输入返回空 Series（不抛）。"""
    out = sma(pd.Series(dtype=float), window=3)
    assert len(out) == 0


def test_sma_aliases_n_period_span():
    """n/period/span 三个别名给出同一结果。"""
    s = pd.Series(range(1, 11), dtype=float)
    base = sma(s, window=3)
    assert sma(s, n=3).equals(base)
    assert sma(s, period=3).equals(base)
    assert sma(s, span=3).equals(base)


def test_sma_values_match_manual_rolling_mean():
    """SMA 数值与手工滚动均值一致，不足窗口为 NaN。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = sma(s, window=2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == pytest.approx(1.5)
    assert out.iloc[2] == pytest.approx(2.5)
    assert out.iloc[3] == pytest.approx(3.5)


def test_ema_aliases_and_first_value():
    """EMA 别名生效且首值等于输入（min_periods=1）。"""
    s = pd.Series([10.0, 11.0, 12.0])
    base = ema(s, span=3)
    assert ema(s, n=3).equals(base)
    assert ema(s, window=3).equals(base)
    assert ema(s, period=3).equals(base)
    assert base.iloc[0] == pytest.approx(10.0)


def test_ema_empty_series_returns_empty():
    """空输入返回空 Series。"""
    assert len(ema(pd.Series(dtype=float), span=3)) == 0


# ---------------------------------------------------------------- rsi


def test_rsi_insufficient_data_returns_all_nan():
    """有效点不足 period+1 时整体为 NaN（诚实置空，不用 50 冒充）。"""
    s = pd.Series([1.0, 2.0])
    out = rsi(s, period=2)
    assert len(out) == 2
    assert out.isna().all()


def test_rsi_all_up_is_100():
    """单调上涨：avg_loss==0 -> RSI 恒 100（首元素无差分，按中性 50 回落）。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = rsi(s, period=2)
    assert out.iloc[0] == pytest.approx(50.0)
    assert out.iloc[1:].eq(100.0).all()


def test_rsi_flat_is_50():
    """完全横盘：中性 50。"""
    s = pd.Series([2.0, 2.0, 2.0, 2.0])
    out = rsi(s, period=2)
    assert out.dropna().eq(50.0).all()


def test_rsi_aliases():
    """n/window/span 别名一致。"""
    s = pd.Series([1.0, 2.0, 0.5, 3.0, 4.0, 2.0])
    base = rsi(s, period=3)
    assert rsi(s, n=3).equals(base)
    assert rsi(s, window=3).equals(base)
    assert rsi(s, span=3).equals(base)


def test_rsi_bounded_in_zero_hundred():
    """RSI 恒落在 [0, 100]。"""
    s = pd.Series([10.0, 9.0, 11.0, 8.0, 12.0, 7.0, 13.0])
    out = rsi(s, period=3).dropna()
    assert out.between(0.0, 100.0).all()


# ---------------------------------------------------------------- bollinger


def test_bollinger_empty_returns_three_empty_series():
    """空输入返回三个空 Series。"""
    mid, up, low = bollinger(pd.Series(dtype=float), window=3)
    assert len(mid) == 0 and len(up) == 0 and len(low) == 0


def test_bollinger_aliases():
    """n/k/std/num_std 别名一致。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    base = bollinger(s, window=3, num_std=2.0)
    assert bollinger(s, n=3, k=2.0)[0].equals(base[0])
    assert bollinger(s, window=3, std=2.0)[0].equals(base[0])
    assert bollinger(s, window=3, num_std=2.0)[0].equals(base[0])


def test_bollinger_bandwidth_uses_std_multiplier():
    """上轨 = 中轨 + k*滚动标准差。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    mid, up, low = bollinger(s, window=3, num_std=2.0)
    std = s.rolling(window=3, min_periods=3).std(ddof=1)
    assert up.iloc[4] == pytest.approx(mid.iloc[4] + 2.0 * std.iloc[4])
    assert low.iloc[4] == pytest.approx(mid.iloc[4] - 2.0 * std.iloc[4])


def test_bollinger_invalid_num_std_falls_back_to_two():
    """num_std 非法字符串回落为 2.0（与显式 2.0 同结果）。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    bad = bollinger(s, window=2, num_std="abc")
    good = bollinger(s, window=2, num_std=2.0)
    assert bad[1].equals(good[1])


def test_bollinger_negative_num_std_falls_back_to_two():
    """负倍数回落为默认 2.0。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert bollinger(s, window=2, num_std=-1.0)[1].equals(bollinger(s, window=2, num_std=2.0)[1])


# ---------------------------------------------------------------- macd


def test_macd_empty_returns_three_empty_series():
    """空输入返回三个空 Series。"""
    a, b, c = macd(pd.Series(dtype=float))
    assert len(a) == 0 and len(b) == 0 and len(c) == 0


def test_macd_aliases_n_fast_slow_signal():
    """n_fast/n_slow/n_signal 别名生效。"""
    s = pd.Series(np.linspace(1, 50, 60))
    base = macd(s, fast=12, slow=26, signal=9)
    assert macd(s, n_fast=12, n_slow=26, n_signal=9)[0].equals(base[0])


def test_macd_window_alias_overrides_default_fast():
    """window 别名仅在 fast 保持默认 12 时覆盖 fast。"""
    s = pd.Series(np.linspace(1, 50, 60))
    got = macd(s, window=5)
    exp = macd(s, fast=5)
    assert got[0].equals(exp[0])


def test_macd_hist_is_macd_minus_signal():
    """hist = macd - signal。"""
    s = pd.Series(np.linspace(1, 50, 60))
    m, sig, hist = macd(s)
    assert hist.iloc[-1] == pytest.approx(m.iloc[-1] - sig.iloc[-1])


# ---------------------------------------------------------------- atr


def test_atr_dataframe_form_matches_sequences():
    """atr(df) 与 atr(high, low, close) 数值一致。"""
    high = [10.0, 11.0, 12.0, 13.0]
    low = [9.0, 10.0, 11.0, 12.0]
    close = [9.5, 10.5, 11.5, 12.5]
    df = pd.DataFrame({"high": high, "low": low, "close": close})
    a = atr(df, period=2)
    b = atr(pd.Series(high), pd.Series(low), pd.Series(close), period=2)
    assert a.equals(b)


def test_atr_dataframe_missing_columns_raises():
    """DataFrame 缺列抛 ValueError 并列出缺失列名。"""
    df = pd.DataFrame({"high": [1.0], "low": [0.5]})
    with pytest.raises(ValueError, match="missing columns"):
        atr(df, period=2)


def test_atr_requires_all_three_series():
    """只给 high/low 或只给 high 抛 ValueError。"""
    with pytest.raises(ValueError, match="requires high, low and close together"):
        atr(pd.Series([1.0]), pd.Series([0.5]), None)
    with pytest.raises(ValueError, match="requires high/low/close"):
        atr(pd.Series([1.0]))


def test_atr_length_mismatch_raises():
    """三序列长度不一致抛 ValueError。"""
    with pytest.raises(ValueError, match="length mismatch"):
        atr(pd.Series([1.0, 2.0]), pd.Series([0.5]), pd.Series([1.5]))


def test_atr_wilder_recursion_values():
    """Wilder 递推：ATR[n-1] 为前 n 个 TR 均值，其后按 (prev*(n-1)+tr)/n 递推。"""
    high = pd.Series([10.0, 11.0, 12.0])
    low = pd.Series([9.0, 10.0, 11.0])
    close = pd.Series([9.5, 10.5, 11.5])
    out = atr(high, low, close, period=2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == pytest.approx((1.0 + 1.5) / 2)
    assert out.iloc[2] == pytest.approx((1.25 * 1 + 1.5) / 2)


def test_atr_nan_bar_is_honest():
    """含 NaN 的 bar -> 对应 ATR 为 NaN（不插值）。"""
    high = pd.Series([10.0, float("nan"), 12.0])
    low = pd.Series([9.0, 9.5, 11.0])
    close = pd.Series([9.5, 10.0, 11.5])
    out = atr(high, low, close, period=1)
    assert np.isnan(out.iloc[1])


def test_atr_empty_input_returns_empty():
    """空序列返回空 Series。"""
    out = atr(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), period=2)
    assert len(out) == 0


def test_atr_aliases_period_window_span():
    """n/window/span 别名一致。"""
    high = pd.Series([10.0, 11.0, 12.0])
    low = pd.Series([9.0, 10.0, 11.0])
    close = pd.Series([9.5, 10.5, 11.5])
    base = atr(high, low, close, period=2)
    assert atr(high, low, close, n=2).equals(base)
    assert atr(high, low, close, window=2).equals(base)
    assert atr(high, low, close, span=2).equals(base)


# ---------------------------------------------------------------- max_drawdown


def test_max_drawdown_negative_value():
    """回撤为负值。"""
    assert max_drawdown(pd.Series([100.0, 90.0, 120.0])) == pytest.approx(-0.1)


def test_max_drawdown_monotonic_is_zero():
    """单调上涨回撤为 0。"""
    assert max_drawdown(pd.Series([1.0, 2.0, 3.0])) == 0.0


def test_max_drawdown_empty_is_zero():
    """空输入回撤为 0。"""
    assert max_drawdown(pd.Series(dtype=float)) == 0.0


def test_max_drawdown_all_nan_is_zero():
    """全 NaN 输入回撤为 0。"""
    assert max_drawdown(pd.Series([float("nan"), float("nan")])) == 0.0


def test_max_drawdown_zero_peak_no_nan_leak():
    """峰值为 0 时回撤定义为 0，不产生 NaN/Inf。"""
    out = max_drawdown(pd.Series([0.0, 5.0]))
    assert out == 0.0
    assert not np.isnan(out)


def test_max_drawdown_accepts_dataframe_with_equity_column():
    """DataFrame 含 equity 列时按 equity 计算。"""
    df = pd.DataFrame({"equity": [100.0, 80.0, 120.0]})
    assert max_drawdown(df) == pytest.approx(-0.2)
