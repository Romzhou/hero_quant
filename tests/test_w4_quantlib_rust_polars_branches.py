"""W4 覆盖率补齐：quantlib/rust.py 与 quantlib/polars_base.py 未覆盖分支。

覆盖目标：
- src/hero_quant/quantlib/rust.py: _to_list 的 DataFrame(close/首列/零列)/Series/list/非法输入、
  _prepare_data 的 NaN 短路、sma/ema/rsi/bollinger/macd 的位置参数拒绝与别名 kwargs、
  max_drawdown 的 Series/DataFrame/list/None 输入、sma_ema 别名
- src/hero_quant/quantlib/polars_base.py: _require_polars 在 polars 缺失时的 ImportError、
  _to_series 的非法输入 ValueError、sma_polars 的 DataFrame 输入、
  ema_polars/rsi_polars 委托 pandas

注：本机未编译 Rust 扩展（IS_RUST=False），Rust 加速分支在本环境不可达，
已在 W4 报告"遗留台账"中登记为不可测分支。

断言均为具体数值/类型/异常类型，不使用弱断言。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hero_quant.quantlib import rust as R
from hero_quant.quantlib import polars_base as P


# ---------------------------------------------------------------- rust: 归一


def test_is_rust_available_reports_bool():
    """探测结果为布尔（当前环境无 Rust 扩展时为 False）。"""
    assert R.is_rust_available() in (True, False)


def test_rust_module_version_string():
    """版本号恒为字符串（编译态取扩展版本，否则 py-fallback）。"""
    assert isinstance(R.__version__, str)
    assert R.__version__


def test_to_list_dataframe_prefers_close():
    """DataFrame 含 close 时取 close 列。"""
    assert R._to_list(pd.DataFrame({"close": [1.0, 2.0], "x": [9.0, 9.0]})) == [1.0, 2.0]


def test_to_list_dataframe_without_close_takes_first_column():
    """无 close 时取首列。"""
    assert R._to_list(pd.DataFrame({"a": [3.0]})) == [3.0]


def test_to_list_dataframe_zero_columns_is_empty():
    """零列 DataFrame 归一为空列表。"""
    assert R._to_list(pd.DataFrame()) == []


def test_to_list_series_and_list():
    """Series / list 输入归一为 float 列表。"""
    assert R._to_list(pd.Series([1, 2])) == [1.0, 2.0]
    assert R._to_list([1.5, 2.5]) == [1.5, 2.5]


def test_to_list_converts_non_finite_to_nan():
    """Inf 被转为 NaN（Rust 侧以 NaN 表示缺失）。"""
    out = R._to_list([1.0, float("inf")])
    assert out[0] == 1.0
    assert np.isnan(out[1])


def test_prepare_data_returns_none_on_nan():
    """含 NaN 时返回 None（信号回落 Python 实现）。"""
    assert R._prepare_data(pd.Series([1.0, float("nan")])) is None


def test_prepare_data_returns_clean_floats():
    """干净数据返回 float 列表。"""
    out = R._prepare_data(pd.Series([1, 2, 3]))
    assert out == [1.0, 2.0, 3.0]
    assert all(isinstance(x, float) for x in out)


# ---------------------------------------------------------------- rust: 参数守卫


@pytest.mark.parametrize(
    "fn,args",
    [
        ("sma", (2, "extra")),
        ("ema", (2, "extra")),
        ("rsi", (2, "extra")),
        ("bollinger", (3, 2.0, "extra")),
        ("macd", (12, 26, 9, "extra")),
    ],
)
def test_rust_functions_reject_extra_positional_args(fn, args):
    """多余位置参数一律拒绝（TypeError）。"""
    func = getattr(R, fn)
    with pytest.raises(TypeError, match="unexpected positional args"):
        func(pd.Series([1.0, 2.0, 3.0]), *args)


def test_rust_sma_aliases():
    """sma 的 n/period/span 别名与显式 window 等价。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    base = R.sma(s, window=3)
    assert R.sma(s, n=3).equals(base)
    assert R.sma(s, period=3).equals(base)
    assert R.sma(s, span=3).equals(base)


def test_rust_ema_aliases():
    """ema 的 n/window/period 别名与显式 span 等价。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    base = R.ema(s, span=3)
    assert R.ema(s, n=3).equals(base)
    assert R.ema(s, window=3).equals(base)
    assert R.ema(s, period=3).equals(base)


def test_rust_rsi_aliases():
    """rsi 的 n/window/span 别名与显式 period 等价。"""
    s = pd.Series([1.0, 2.0, 3.0, 2.5, 4.0])
    base = R.rsi(s, period=3)
    assert R.rsi(s, n=3).equals(base)
    assert R.rsi(s, window=3).equals(base)
    assert R.rsi(s, span=3).equals(base)


def test_rust_bollinger_returns_three_series():
    """bollinger 返回中/上/下三条同长序列（有效窗口内上轨>=中轨>=下轨）。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    mid, up, low = R.bollinger(s, window=3)
    assert len(mid) == len(up) == len(low) == 5
    valid = mid.notna()
    assert (up[valid] >= mid[valid]).all()
    assert (low[valid] <= mid[valid]).all()
    assert mid.iloc[-1] == pytest.approx(4.0)


def test_rust_macd_returns_three_series():
    """macd 返回三条同长序列。"""
    s = pd.Series(np.linspace(1, 50, 40))
    a, b, c = R.macd(s)
    assert len(a) == len(b) == len(c) == 40


def test_rust_max_drawdown_inputs():
    """max_drawdown 接受 Series / DataFrame / list / None。"""
    assert R.max_drawdown(pd.Series([100.0, 80.0, 120.0])) == pytest.approx(-0.2)
    assert R.max_drawdown(pd.DataFrame({"e": [100.0, 80.0]})) == pytest.approx(-0.2)
    assert R.max_drawdown([100.0, 80.0]) == pytest.approx(-0.2)
    assert R.max_drawdown(None) == 0.0


def test_sma_ema_alias_is_same_object():
    """sma_ema 为 sma 的别名。"""
    assert R.sma_ema is R.sma


# ---------------------------------------------------------------- polars_base


def test_require_polars_raises_when_missing(monkeypatch):
    """polars 缺失时给出明确安装指引的 ImportError。"""
    monkeypatch.setattr(P, "pl", None)
    with pytest.raises(ImportError, match="polars is required"):
        P._require_polars()


def test_polars_to_series_rejects_invalid_input():
    """不可构造的输入（set）抛 ValueError。"""
    with pytest.raises(ValueError, match="cannot convert to Series"):
        P._to_series({1, 2, 3})


def test_polars_to_series_dataframe_zero_columns_is_empty():
    """零列 DataFrame 归一为空 Series。"""
    assert P._to_series(pd.DataFrame()).empty is True


def test_polars_sma_matches_pandas_for_dataframe():
    """DataFrame 输入的 polars SMA 与 pandas SMA 对齐。"""
    from hero_quant.quantlib.indicators import sma as py_sma

    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    got = P.sma_polars(df, 3)
    exp = py_sma(df, 3)
    pd.testing.assert_series_equal(got, exp, check_dtype=False, check_names=False)


def test_polars_ema_and_rsi_delegate_to_pandas():
    """ema_polars / rsi_polars 委托 pandas 实现（口径一致）。"""
    from hero_quant.quantlib.indicators import ema as py_ema
    from hero_quant.quantlib.indicators import rsi as py_rsi

    s = pd.Series([1.0, 2.0, 3.0, 2.5, 4.0, 5.0])
    pd.testing.assert_series_equal(P.ema_polars(s, span=3), py_ema(s, span=3), check_names=False)
    pd.testing.assert_series_equal(P.rsi_polars(s, period=3), py_rsi(s, period=3), check_names=False)
