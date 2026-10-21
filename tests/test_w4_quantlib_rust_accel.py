"""W4 覆盖率补齐：quantlib/rust.py 的 Rust 加速分支（本机无编译扩展，用假模块驱动）。

覆盖目标（src/hero_quant/quantlib/rust.py）：
- _use_rust() 为真时的 sma/ema/rsi/bollinger/macd 加速路径：
  None -> NaN 映射、Series/DataFrame/list 三种索引来源、异常回落 Python
- bollinger 传 n/k 别名时跳过 Rust 直达 Python
- max_drawdown 的 Rust 路径：Series / DataFrame / list / None 输入
- _prepare_data 含 NaN 时短路回落 Python
- _to_list 的 DataFrame(close/首列/零列) 与非法输入

注：本机未编译 Rust 扩展（IS_RUST=False），故用假模块显式打开加速分支；
断言均为具体数值/长度/异常类型，不使用弱断言。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hero_quant.quantlib import rust as R


class _FakeRust:
    """行为可控的假 Rust 扩展（记录调用参数，便于断言真的走了加速路径）。"""

    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.calls: list[tuple] = []

    def sma(self, data, window):
        self.calls.append(("sma", window))
        if "sma" in self.fail:
            raise RuntimeError("rust sma boom")
        return [None if i < window - 1 else sum(data[i - window + 1:i + 1]) / window for i in range(len(data))]

    def ema(self, data, span):
        self.calls.append(("ema", span))
        if "ema" in self.fail:
            raise RuntimeError("rust ema boom")
        return list(data)

    def rsi(self, data, period):
        self.calls.append(("rsi", period))
        if "rsi" in self.fail:
            raise RuntimeError("rust rsi boom")
        return [50.0] * len(data)

    def bollinger(self, data, window, num_std):
        self.calls.append(("bollinger", window, num_std))
        if "bollinger" in self.fail:
            raise RuntimeError("rust bollinger boom")
        n = len(data)
        mid = [None if i < window - 1 else sum(data[i + 1 - window:i + 1]) / window for i in range(n)]
        up = [None if v is None else v + num_std for v in mid]
        low = [None if v is None else v - num_std for v in mid]
        return mid, up, low

    def macd(self, data, fast, slow, signal):
        self.calls.append(("macd", fast, slow, signal))
        if "macd" in self.fail:
            raise RuntimeError("rust macd boom")
        n = len(data)
        return ([1.0] * n, [2.0] * n, [3.0] * n)

    def max_drawdown(self, data):
        self.calls.append(("max_drawdown",))
        if "max_drawdown" in self.fail:
            raise RuntimeError("rust mdd boom")
        peak = -math.inf
        mdd = 0.0
        for v in data:
            peak = max(peak, v)
            if peak:
                mdd = min(mdd, v / peak - 1.0)
        return mdd


@pytest.fixture
def rust_on(monkeypatch):
    """打开 Rust 加速分支并在结束后还原。"""
    fake = _FakeRust()
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    return fake


# ---------------------------------------------------------------- sma


def test_sma_uses_rust_path_and_maps_none_to_nan(rust_on):
    """干净输入走 Rust 加速路径；None 映射为 NaN 且不足窗口保持 NaN。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = R.sma(s, window=3)
    assert rust_on.calls == [("sma", 3)]
    assert np.isnan(out.iloc[0])
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_sma_rust_path_preserves_series_index(rust_on):
    """Series 输入保留原始索引。"""
    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    s = pd.Series([1.0, 2.0, 3.0, 4.0], index=idx)
    out = R.sma(s, window=2)
    assert list(out.index) == list(idx)


def test_sma_rust_path_preserves_dataframe_index(rust_on):
    """DataFrame 输入保留原始索引。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    out = R.sma(df, window=2)
    assert list(out.index) == list(idx)


def test_sma_rust_path_with_plain_list_has_range_index(rust_on):
    """普通 list 输入的 Rust 结果使用默认 RangeIndex。"""
    out = R.sma([1.0, 2.0, 3.0], window=2)
    assert list(out.index) == [0, 1, 2]


def test_sma_nan_input_falls_back_to_python(rust_on):
    """含 NaN 的输入短路回落 Python 实现（不污染 Rust 语义）。"""
    s = pd.Series([1.0, float("nan"), 3.0, 4.0])
    out = R.sma(s, window=2)
    assert rust_on.calls == []
    assert np.isnan(out.iloc[1])
    assert out.iloc[3] == pytest.approx(3.5)


def test_sma_rust_exception_falls_back_to_python(monkeypatch):
    """Rust 侧抛异常时回落 Python 实现（不抛给调用方）。"""
    fake = _FakeRust(fail={"sma"})
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = R.sma(s, window=2)
    assert len(out) == 4
    assert out.iloc[-1] == pytest.approx(3.5)


# ---------------------------------------------------------------- ema / rsi


def test_ema_uses_rust_path(rust_on):
    """干净输入走 ema 加速路径。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = R.ema(s, span=2)
    assert rust_on.calls == [("ema", 2)]
    assert list(out) == [1.0, 2.0, 3.0, 4.0]


def test_ema_rust_exception_falls_back(monkeypatch):
    """Rust ema 异常时回落 Python。"""
    fake = _FakeRust(fail={"ema"})
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    out = R.ema(pd.Series([1.0, 2.0, 3.0]), span=2)
    assert len(out) == 3


def test_ema_nan_input_falls_back_to_python(rust_on):
    """含 NaN 输入短路回落 Python。"""
    out = R.ema(pd.Series([1.0, float("nan")]), span=2)
    assert rust_on.calls == []
    assert len(out) == 2


def test_rsi_uses_rust_path(rust_on):
    """干净输入走 rsi 加速路径，返回固定 50。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = R.rsi(s, period=2)
    assert rust_on.calls == [("rsi", 2)]
    assert list(out) == [50.0, 50.0, 50.0, 50.0]


def test_rsi_rust_exception_falls_back(monkeypatch):
    """Rust rsi 异常时回落 Python。"""
    fake = _FakeRust(fail={"rsi"})
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    out = R.rsi(pd.Series([1.0, 2.0, 3.0, 4.0, 3.0]), period=2)
    assert len(out) == 5


# ---------------------------------------------------------------- bollinger


def test_bollinger_uses_rust_path(rust_on):
    """干净输入走 bollinger 加速路径。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    mid, up, low = R.bollinger(s, window=3, num_std=2.0)
    assert rust_on.calls == [("bollinger", 3, 2.0)]
    assert len(mid) == len(up) == len(low) == 5
    assert np.isnan(mid.iloc[0])
    assert mid.iloc[4] == pytest.approx(4.0)
    assert up.iloc[4] == pytest.approx(6.0)
    assert low.iloc[4] == pytest.approx(2.0)


def test_bollinger_with_alias_skips_rust(rust_on):
    """传 n/k 别名时跳过 Rust 加速路径（走 Python）。"""
    mid, _up, _low = R.bollinger(pd.Series([1.0, 2.0, 3.0, 4.0]), window=2, n=2)
    assert rust_on.calls == []
    assert len(mid) == 4


def test_bollinger_nan_input_falls_back(rust_on):
    """含 NaN 输入短路回落 Python。"""
    mid, _up, _low = R.bollinger(pd.Series([1.0, float("nan"), 3.0]), window=2)
    assert rust_on.calls == []
    assert len(mid) == 3


def test_bollinger_rust_exception_falls_back(monkeypatch):
    """Rust bollinger 异常时回落 Python。"""
    fake = _FakeRust(fail={"bollinger"})
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    mid, _up, _low = R.bollinger(pd.Series([1.0, 2.0, 3.0, 4.0]), window=2)
    assert len(mid) == 4


# ---------------------------------------------------------------- macd


def test_macd_uses_rust_path(rust_on):
    """干净输入走 macd 加速路径。"""
    s = pd.Series(np.linspace(1, 30, 30))
    a, b, c = R.macd(s)
    assert rust_on.calls == [("macd", 12, 26, 9)]
    assert a.iloc[0] == pytest.approx(1.0)
    assert b.iloc[0] == pytest.approx(2.0)
    assert c.iloc[0] == pytest.approx(3.0)


def test_macd_nan_input_falls_back(rust_on):
    """含 NaN 输入短路回落 Python。"""
    a, _b, _c = R.macd(pd.Series([1.0, float("nan"), 3.0, 4.0]))
    assert rust_on.calls == []
    assert len(a) == 4


def test_macd_rust_exception_falls_back(monkeypatch):
    """Rust macd 异常时回落 Python。"""
    fake = _FakeRust(fail={"macd"})
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    a, _b, _c = R.macd(pd.Series(np.linspace(1, 30, 30)))
    assert len(a) == 30


# ---------------------------------------------------------------- max_drawdown


def test_max_drawdown_rust_path_series(rust_on):
    """Rust 路径计算最大回撤。"""
    out = R.max_drawdown(pd.Series([100.0, 80.0, 120.0]))
    assert rust_on.calls == [("max_drawdown",)]
    assert out == pytest.approx(-0.2)


def test_max_drawdown_rust_path_dataframe(rust_on):
    """DataFrame 输入取首列送入 Rust。"""
    out = R.max_drawdown(pd.DataFrame({"e": [100.0, 90.0]}))
    assert out == pytest.approx(-0.1)


def test_max_drawdown_rust_path_list(rust_on):
    """普通 list 输入送入 Rust。"""
    out = R.max_drawdown([100.0, 90.0])
    assert out == pytest.approx(-0.1)


def test_max_drawdown_rust_path_none_gives_empty(rust_on):
    """None 输入在 Rust 路径下按空数据处理。"""
    out = R.max_drawdown(None)
    assert out == pytest.approx(0.0)


def test_max_drawdown_rust_exception_falls_back(monkeypatch):
    """Rust max_drawdown 异常时回落 Python 实现。"""
    fake = _FakeRust(fail={"max_drawdown"})
    monkeypatch.setattr(R, "_RUST_MOD", fake)
    monkeypatch.setattr(R, "IS_RUST", True)
    out = R.max_drawdown(pd.Series([100.0, 80.0, 120.0]))
    assert out == pytest.approx(-0.2)


def test_use_rust_requires_both_flags():
    """_use_rust 需同时满足 IS_RUST 与 _RUST_MOD 非空。"""
    import hero_quant.quantlib.rust as mod

    orig_rust, orig_mod = mod.IS_RUST, mod._RUST_MOD
    try:
        mod.IS_RUST = True
        mod._RUST_MOD = None
        assert mod._use_rust() is False
        mod.IS_RUST = False
        mod._RUST_MOD = object()
        assert mod._use_rust() is False
    finally:
        mod.IS_RUST = orig_rust
        mod._RUST_MOD = orig_mod
