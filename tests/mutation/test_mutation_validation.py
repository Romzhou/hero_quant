# tests/mutation/test_mutation_validation.py
"""N线变异试点: backtest/validation.py 高价值存活 mutant 的定向杀伤测试 (P0 fail-closed).

只新增本目录文件, 不改现有测试与 src. 每个用例注释标出所杀 mutant.
约定: 纯 pandas 确定性断言, 无外部依赖; 时区用固定偏移 (Etc/GMT±) 避免 DST.
"""

import numpy as np
import pandas as pd
import pytest

from hero_quant.backtest.validation import ValidationError, validate


def _frame(close=None):
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    c = 100.0 + np.arange(10.0) if close is None else close
    return pd.DataFrame({"close": c}, index=idx)


def test_pit_partial_args_skip():
    """钉 validation.py:102 (已证等价: pandas3 Timestamp(None)=NaT, NaT 比较恒 False, or 分支不可观测).

    契约本身仍锁定: 缺一边时间戳即跳过 PIT, 不得崩溃或误杀.
    """
    df = _frame()
    assert validate(df, weights_on="2024-01-05") is None
    assert validate(df, price_date="2024-01-05") is None
    # 缺一边即跳过: 未来权重 / 过去价格也不得误杀或崩溃
    assert validate(df, weights_on="2030-01-05") is None
    assert validate(df, price_date="2020-01-01") is None


def test_pit_mixed_same_instant_passes():
    """钉 validation.py:119/128/131 同一时刻不同表示: naive 与 UTC-aware 同一瞬间必须放行."""
    df = _frame()
    assert (
        validate(
            df,
            weights_on="2024-01-05",
            price_date=pd.Timestamp("2024-01-05", tz="UTC"),
        )
        is None
    )


def test_pit_mixed_same_wall_east_rejected():
    """杀 validation.py:119/128/131 同墙钟+非零东偏移: 双向都必须 fail-closed 拒绝."""
    df = _frame()
    east = pd.Timestamp("2024-01-05 12:00", tz="Etc/GMT-5")  # UTC+5
    with pytest.raises(ValidationError):
        validate(df, weights_on="2024-01-05 12:00", price_date=east)
    with pytest.raises(ValidationError):
        validate(df, weights_on=east, price_date="2024-01-05 12:00")


def test_pit_mixed_same_wall_west_rejected():
    """杀 validation.py:119 [ROR] > / >= (东向测试杀不掉的方向): 西偏移同墙钟也必须拒绝."""
    df = _frame()
    west = pd.Timestamp("2024-01-05 12:00", tz="Etc/GMT+5")  # UTC-5
    with pytest.raises(ValidationError):
        validate(df, weights_on="2024-01-05 12:00", price_date=west)
    with pytest.raises(ValidationError):
        validate(df, weights_on=west, price_date="2024-01-05 12:00")


def test_pit_mixed_valid_ordering_passes():
    """杀 validation.py:128 [ROR] == -> <= : 排序合法的混合对必须放行, 不得误杀."""
    df = _frame()
    assert (
        validate(
            df,
            weights_on="2024-01-05",
            price_date=pd.Timestamp("2024-01-06 12:00", tz="Etc/GMT-5"),
        )
        is None
    )


def test_pit_mixed_west_wall_later_passes():
    """杀 validation.py:128 [ROR] == -> >= : 墙钟晚但瞬间早(西偏移)的合法对必须放行."""
    df = _frame()
    assert (
        validate(
            df,
            weights_on="2024-01-05 04:00",
            price_date=pd.Timestamp("2024-01-05 00:00", tz="Etc/GMT+5"),  # UTC-5, 瞬间为 05:00Z
        )
        is None
    )


def test_multi_asset_no_close_passes():
    """杀 validation.py:146 [LOR] and -> or : 无 close 列的多资产帧走逐列校验, 不得 KeyError."""
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame(
        {"a": 100.0 + np.arange(10.0), "b": 50.0 + np.arange(10.0)},
        index=idx,
    )
    assert validate(df) is None


def test_sibling_dirty_open_rejected():
    """杀 validation.py:147 [ROR] c != 'close' 全家 (P0 高): close 干净但兄弟价格列为脏必须拒绝.

    背景: engine 用次日 open 作为可执行价, 跳过兄弟列校验等于放行脏价格进回测.
    """
    df = _frame()
    df["open"] = 100.0 + np.arange(10.0)
    df.loc[df.index[3], "open"] = -5.0
    with pytest.raises(ValidationError):
        validate(df)


def test_sibling_dirty_adjclose_rejected():
    """杀 validation.py:147 [ROR] c != 'close' -> c > 'close' / c >= 'close'.

    字典序小于 'close' 的兄弟列 (如 adjclose) 在 >/>= mutant 下会被跳过校验, 必须拒绝.
    """
    df = _frame()
    df["adjclose"] = 100.0 + np.arange(10.0)
    df.loc[df.index[3], "adjclose"] = -5.0
    with pytest.raises(ValidationError):
        validate(df)


def test_sub_unit_prices_pass():
    """杀 validation.py:157/184 [CONST] <= 0 -> <= 1 : 亚 1 元价格 (penny stocks) 必须放行."""
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    sub = pd.DataFrame(
        {"close": [0.5, 0.6, 0.55, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]}, index=idx
    )
    assert validate(sub) is None
    multi = pd.DataFrame(
        {
            "a": [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4],
            "b": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2],
        },
        index=idx,
    )
    assert validate(multi) is None


def test_multi_asset_zero_price_rejected():
    """杀 validation.py:184 [LOR] or -> and : 多资产零价格必须拒绝, 不得因 and 短路放行."""
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame(
        {"a": 100.0 + np.arange(10.0), "b": 50.0 + np.arange(10.0)},
        index=idx,
    )
    df.loc[df.index[2], "b"] = 0.0
    with pytest.raises(ValidationError):
        validate(df)


def test_multi_asset_negative_price_rejected():
    """杀 validation.py:184 [ROR] series <= 0 -> series == 0 : 负价格在 == mutant 下会被放行, 必须拒绝."""
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame(
        {"a": 100.0 + np.arange(10.0), "b": 50.0 + np.arange(10.0)},
        index=idx,
    )
    df.loc[df.index[2], "b"] = -3.0
    with pytest.raises(ValidationError):
        validate(df)


def test_currency_lexicographic_mismatch_rejected():
    """杀 validation.py:213 [ROR] == -> <= : 字典序下成立的币种失配 (AUD vs CNY) 必须拒绝."""
    df = _frame()
    df["currency"] = "AUD"
    with pytest.raises(ValidationError):
        validate(df, currency="CNY")


def test_currency_single_matching_passes():
    """pin validation.py:213: 单一币种与显式 currency 一致必须放行 (id76 匹配侧缺口)."""
    df = _frame()
    df["currency"] = "CNY"
    assert validate(df, currency="CNY") is None
