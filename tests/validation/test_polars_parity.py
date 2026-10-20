"""U1: polars 引擎路径 vs 默认(pandas)路径交叉验证.

范围: `src/hero_quant/quantlib/polars_base.py` (sma_polars/ema_polars/rsi_polars)
  vs `src/hero_quant/quantlib/indicators.py` (sma/ema/rsi).
方法: 手算 golden + pandas 独立实现三方对照, 同一输入同一输出.
  本机无 polars 包时 sma 数值对照 skip, 其余委托路径(ema/rsi)与
  _to_series/_validate_window 接线逻辑仍实测; 另用 Fake `pl` 模块
  验证 min_samples/min_periods 接线与索引/名称保留, 不依赖真实 polars.
只新增、不改存量测试. 见 docs/validation-report.md U1 说明项.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hero_quant.quantlib import indicators as Q
from hero_quant.quantlib import polars_base as P

try:
    import polars as _pl  # noqa: F401

    _HAS_POLARS = True
except ImportError:
    _HAS_POLARS = False

needs_polars = pytest.mark.skipif(not _HAS_POLARS, reason="polars not installed; vector extras required")


def _assert_series_equal(a: pd.Series, b: pd.Series) -> None:
    """NaN 感知的 Series 等价断言."""
    assert len(a) == len(b)
    assert list(a.index) == list(b.index)
    pd.testing.assert_series_equal(pd.Series(a.values, index=a.index), pd.Series(b.values, index=b.index), check_dtype=False)


def test_sma_golden_pandas_baseline():
    """ Hand golden: SMA w=3 on [1,2,3,4,5] -> [nan,nan,2,3,4]."""
    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    out = Q.sma(s, 3).tolist()
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2:] == [2.0, 3.0, 4.0]


@needs_polars
def test_sma_polars_matches_pandas_golden():
    """同一输入同一输出: sma_polars == sma == 手算 golden."""
    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    a = Q.sma(s, 3)
    b = P.sma_polars(s, 3)
    _assert_series_equal(a, b)
    assert math.isnan(float(b.iloc[0])) and math.isnan(float(b.iloc[1]))
    assert b.tolist()[2:] == [2.0, 3.0, 4.0]


@needs_polars
def test_sma_polars_matches_pandas_extended():
    """扩展对照: 长序列/别名窗口/自定义索引/名称保留."""
    rng = np.random.default_rng(11)
    px = pd.Series(100 + np.cumsum(rng.normal(0.2, 1.0, size=60)), dtype=float)
    for w in (1, 5, 20):
        _assert_series_equal(Q.sma(px, w), P.sma_polars(px, w))
    # 别名 n/period/span 单键等价
    s = pd.Series([1.0, 2, 3, 4, 5, 6], dtype=float)
    _assert_series_equal(Q.sma(s, 2), P.sma_polars(s, n=2))
    # 自定义索引与名称保留
    idx = pd.date_range("2026-01-01", periods=6, freq="D")
    named = pd.Series([1.0, 2, 3, 4, 5, 6], index=idx, name="close")
    out = P.sma_polars(named, 3)
    assert list(out.index) == list(idx)
    assert out.name == "close"
    _assert_series_equal(Q.sma(named, 3), out)
    # 窗口大于长度 -> 全 NaN, 两侧一致
    _assert_series_equal(Q.sma(s, 100), P.sma_polars(s, 100))


def test_sma_polars_wiring_mocked_min_samples():
    """无真实 polars 时用 Fake pl 验证接线: min_samples=n + 索引/名称保留."""
    import types

    calls: dict = {}

    class _FakeRolling:
        def __init__(self, vals):
            self._vals = vals

        def to_list(self):
            return list(self._vals)

    class _FakePlSeries:
        def __init__(self, arr):
            self._arr = np.asarray(arr, dtype=float)

        def rolling_mean(self, window_size=None, min_samples=None, min_periods=None):
            calls["window_size"] = window_size
            calls["min_samples"] = min_samples
            calls["min_periods"] = min_periods
            n = window_size
            mp = min_samples if min_samples is not None else min_periods
            calls["effective_min"] = mp
            # 与 pandas rolling(n, min_periods=n).mean() 同语义
            vals = pd.Series(self._arr).rolling(window=n, min_periods=mp).mean().tolist()
            return _FakeRolling(vals)

    fake_pl = types.SimpleNamespace()
    fake_pl.Float64 = object()
    fake_pl.Series = lambda name, arr, dtype=None, strict=False: _FakePlSeries(arr)

    s = pd.Series([1.0, 2, 3, 4, 5], index=pd.RangeIndex(5), name="close")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(P, "pl", fake_pl)
    try:
        out = P.sma_polars(s, 3)
    finally:
        monkey.undo()
    assert calls["window_size"] == 3
    assert calls["min_samples"] == 3
    assert calls["effective_min"] == 3
    assert list(out.index) == [0, 1, 2, 3, 4]
    assert out.name == "close"
    assert math.isnan(float(out.iloc[0])) and math.isnan(float(out.iloc[1]))
    assert out.tolist()[2:] == [2.0, 3.0, 4.0]


def test_sma_polars_wiring_mocked_min_periods_fallback():
    """旧版 polars API 回落: min_samples TypeError -> min_periods 重试."""
    import types

    class _FakeRolling:
        def __init__(self, vals):
            self._vals = vals

        def to_list(self):
            return list(self._vals)

    class _FakePlSeriesOld:
        def __init__(self, arr):
            self._arr = np.asarray(arr, dtype=float)

        def rolling_mean(self, window_size=None, min_samples=None, min_periods=None):
            if min_samples is not None:
                raise TypeError("old polars has no min_samples")
            vals = pd.Series(self._arr).rolling(window=window_size, min_periods=min_periods).mean().tolist()
            return _FakeRolling(vals)

    fake_pl = types.SimpleNamespace()
    fake_pl.Float64 = object()
    fake_pl.Series = lambda name, arr, dtype=None, strict=False: _FakePlSeriesOld(arr)

    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(P, "pl", fake_pl)
    try:
        out = P.sma_polars(s, 3)
    finally:
        monkey.undo()
    assert out.tolist()[2:] == [2.0, 3.0, 4.0]


def test_sma_polars_empty_shortcircuits_without_polars(monkeypatch):
    """空序列短路: 不要求 polars 已装."""
    monkeypatch.setattr(P, "pl", None)
    out = P.sma_polars(pd.Series([], dtype=float))
    assert out.empty


def test_sma_polars_missing_polars_fail_visible(monkeypatch):
    """缺 polars 时 fail-visible: ImportError 带安装指引, 非静默回落."""
    monkeypatch.setattr(P, "pl", None)
    with pytest.raises(ImportError, match="hero-quant\\[vector\\]"):
        P.sma_polars(pd.Series([1.0, 2.0, 3.0]))


def test_ema_polars_parity_hand_golden():
    """ema_polars 委托 pandas: 同一输入 bit 一致 + 手算 golden."""
    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    hand = [1.0, 1.5, 2.25, 3.125, 4.0625]
    assert Q.ema(s, 3).tolist() == hand
    assert P.ema_polars(s, span=3).tolist() == hand
    _assert_series_equal(Q.ema(s, 3), P.ema_polars(s, span=3))
    # 别名 span/n/window/period 等价
    _assert_series_equal(P.ema_polars(s, span=3), P.ema_polars(s, n=3))
    # 空/NaN/Inf 鲁棒性两侧一致
    empty = pd.Series([], dtype=float)
    assert P.ema_polars(empty).empty
    tricky = pd.Series([1.0, np.nan, float("inf"), 4.0, 5.0])
    _assert_series_equal(Q.ema(tricky, 3), P.ema_polars(tricky, span=3))


def test_rsi_polars_parity_boundaries_and_convergence():
    """rsi_polars 委托 pandas: 边界 100/50 + 混合序列 Wilder 收敛."""
    up = pd.Series([float(i) for i in range(1, 21)], dtype=float)
    assert float(P.rsi_polars(up, period=14).iloc[-1]) == 100.0
    flat = pd.Series([10.0] * 20)
    assert float(P.rsi_polars(flat, period=14).iloc[-1]) == 50.0
    # 同一输入同一输出
    _assert_series_equal(Q.rsi(up, 14), P.rsi_polars(up, period=14))
    _assert_series_equal(Q.rsi(flat, 14), P.rsi_polars(flat, period=14))
    # 200 点混合序列尾部收敛(独立 Wilder-SMA-seed 对照, 容差 1e-3)
    rng = np.random.default_rng(7)
    px = pd.Series(100 + np.cumsum(rng.normal(0.2, 1.0, size=200)), dtype=float)
    n = 14
    rq = P.rsi_polars(px, period=n)
    d = px.diff()
    g = d.where(d > 0, 0.0)
    ell = -d.where(d < 0, 0.0)
    ag, al = float(g.iloc[1 : n + 1].mean()), float(ell.iloc[1 : n + 1].mean())
    w = np.full(len(px), np.nan)
    w[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(px)):
        ag = (ag * (n - 1) + float(g.iloc[i])) / n
        al = (al * (n - 1) + float(ell.iloc[i])) / n
        w[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    tail = (rq.iloc[-50:] - pd.Series(w, index=px.index).iloc[-50:]).abs()
    assert float(tail.max()) < 1e-3


def test_to_series_parity_polars_vs_indicators():
    """_to_series 归一化两侧一致: close/equity/首列/list/Inf/空."""
    cases = [
        pd.Series([1.0, 2.0, 3.0], name="close"),
        pd.DataFrame({"close": [1.0, 2.0], "other": [9, 9]}),
        pd.DataFrame({"equity": [4.0, 5.0]}),
        pd.DataFrame({"aaa": [7.0, 8.0]}),
        [1, 2, 3],
        [1.0, float("inf")],
        pd.DataFrame(),
    ]
    for c in cases:
        a = Q._to_series(c)
        b = P._to_series(c)
        assert len(a) == len(b)
        assert int(a.isna().sum()) == int(b.isna().sum())
        pd.testing.assert_series_equal(
            pd.Series(a.values), pd.Series(b.values), check_dtype=False, check_names=False
        )


def test_validate_window_and_alias_contract_documented():
    """窗口/别名契约说明项(非缺陷): polars 更严格, indicators 兼容旧口径.

    - None: polars 回落默认 20, indicators 抛 ValueError(历史行为, J1 已锁定).
    - 冲突别名/多余位置参数: polars 抛 TypeError, indicators 取末值/忽略(历史行为).
    两侧在合法输入上一致; 本用例锁定该说明, 防误修.
    """
    assert P._validate_window(None) == 20
    assert P._validate_window(None, default=7) == 7
    with pytest.raises(ValueError):
        Q._validate_window(None)
    for bad in (0, -3, "bad"):
        with pytest.raises(ValueError):
            P._validate_window(bad)
        with pytest.raises(ValueError):
            Q._validate_window(bad)
    assert P._validate_window(5) == Q._validate_window(5) == 5
    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    # 合法单别名两侧一致(需真实 polars; 缺装时由 mocked 接线用例覆盖)
    if _HAS_POLARS:
        _assert_series_equal(Q.sma(s, 2), P.sma_polars(pd.Series([1.0, 2, 3, 4, 5], dtype=float), n=2))
    # 冲突别名: polars 严格抛错(已由 test_quantlib_vector 锁定)
    with pytest.raises(TypeError, match="conflicting aliases"):
        P.sma_polars(s, n=3, period=5)
    with pytest.raises(TypeError, match="unexpected positional"):
        P.sma_polars(s, 3, 4)
