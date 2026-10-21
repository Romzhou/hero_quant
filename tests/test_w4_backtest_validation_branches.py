"""W4 覆盖率补齐：backtest/validation.py 未覆盖分支。

覆盖目标（src/hero_quant/backtest/validation.py）：
- kwargs 兼容路径、过多位置参数、未知 kwargs 的 fail-closed
- 空帧 / 非 DataFrame / 非 DatetimeIndex / 重复时间戳 / 未排序索引的拒绝
- PIT：naive vs aware 归一比较、同墙钟非 UTC 偏移的 fail-closed、越界与非法时间戳
- 价格：非正价、NaN、非数值、inf、以及多资产（无 close 列）逐列校验
- 币种：NaN 币种、混币种、与显式 currency 不一致、ccy 列（大小写不敏感）
- NON_PRICE_COLS（volume/currency/ccy）不参与价格校验

断言均为具体异常类型/消息片段/数值，不使用弱断言。
"""

from __future__ import annotations

import pandas as pd
import pytest

from hero_quant.backtest.validation import (
    NON_PRICE_COLS,
    PIT_ACK,
    ValidationError,
    validate,
)


def _frame(n: int = 3, start: str = "2024-01-01", col: str = "close", value: float = 10.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({col: [value] * n}, index=idx)


# ---------------------------------------------------------------- 常量契约


def test_pit_ack_constant_is_frozen():
    """PIT 旁路契约字符串全局冻结，不得改动。"""
    assert PIT_ACK == "I_KNOW_THIS_IS_NON_PIT"


def test_non_price_cols_contents():
    """NON_PRICE_COLS 为价格校验跳过的元数据列集合。"""
    assert set(NON_PRICE_COLS) == {"volume", "currency", "ccy"}


# ---------------------------------------------------------------- 参数形态 fail-closed


def test_validate_rejects_too_many_positional_args():
    """多余位置参数直接拒绝（不做错位重映射）。"""
    df = _frame()
    with pytest.raises(ValidationError, match="too many positional args"):
        validate(df, "2024-01-01", "2024-01-01", "USD", "extra")


def test_validate_rejects_unknown_kwargs():
    """未知 kwargs fail-closed（防拼写错误关闭校验）。"""
    df = _frame()
    with pytest.raises(ValidationError, match="unknown kwargs rejected"):
        validate(df, weights_on="2024-01-01", price_date="2024-01-02", weights_onn="typo")


def test_validate_accepts_kwargs_alias_for_same_named_params():
    """同名参数经 kwargs 传入时与位置参数等价（通过）。"""
    df = _frame()
    assert validate(df, weights_on="2024-01-01", price_date="2024-01-02") is None


def test_validate_rejects_empty_frame():
    """空 DataFrame 拒绝（禁止空帧绕过所有校验）。"""
    with pytest.raises(ValidationError, match="empty or not a DataFrame"):
        validate(pd.DataFrame())


def test_validate_rejects_non_dataframe():
    """非 DataFrame 输入拒绝。"""
    with pytest.raises(ValidationError, match="empty or not a DataFrame"):
        validate([1, 2, 3])


def test_validate_rejects_non_datetime_index():
    """非 DatetimeIndex 拒绝（否则 pct_change/ret 错位）。"""
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=["a", "b"])
    with pytest.raises(ValidationError, match="must be DatetimeIndex"):
        validate(df)


def test_validate_rejects_duplicate_timestamps():
    """重复时间戳拒绝（否则收益错位）。"""
    idx = pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-02"])
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    with pytest.raises(ValidationError, match="duplicated timestamps"):
        validate(df)


def test_validate_rejects_unsorted_index():
    """未按时间递增的索引拒绝。"""
    idx = pd.to_datetime(["2024-01-03", "2024-01-01", "2024-01-02"])
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    with pytest.raises(ValidationError, match="not sorted monotonic increasing"):
        validate(df)


# ---------------------------------------------------------------- PIT


def test_validate_pit_violation_rejected():
    """weights_on > price_date 使用未来数据，拒绝。"""
    df = _frame()
    with pytest.raises(ValidationError, match="PIT violation"):
        validate(df, weights_on="2024-01-05", price_date="2024-01-01")


def test_validate_pit_equal_timestamp_allowed():
    """weights_on == price_date 合法。"""
    df = _frame()
    assert validate(df, weights_on="2024-01-01", price_date="2024-01-01") is None


def test_validate_pit_mixed_naive_aware_same_instant_allowed():
    """naive 视为 UTC 归一后与 UTC-aware 是同一瞬时，不算违规。"""
    df = _frame()
    assert validate(df, weights_on="2024-01-01T00:00:00+00:00", price_date="2024-01-01") is None


def test_validate_pit_mixed_naive_aware_same_wallclock_non_utc_rejected():
    """naive 与非 UTC 偏移 aware 同墙钟 -> fail-closed（假设 UTC 会翻转结论）。"""
    df = _frame()
    with pytest.raises(ValidationError, match="mixed naive/aware PIT timestamps"):
        validate(df, weights_on="2024-01-01 00:00:00+08:00", price_date="2024-01-01 00:00:00")


def test_validate_pit_invalid_timestamp_rejected():
    """无法解析的时间戳拒绝。"""
    df = _frame()
    with pytest.raises(ValidationError, match="invalid timestamp"):
        validate(df, weights_on="not-a-date", price_date="2024-01-01")


def test_validate_pit_only_weights_on_skips_comparison():
    """只给 weights_on（无 price_date）时不比较，不抛。"""
    df = _frame()
    assert validate(df, weights_on="2024-01-01") is None


# ---------------------------------------------------------------- 价格校验


def test_validate_rejects_non_positive_close():
    """close <= 0 视为脏数据，拒绝。"""
    df = _frame(value=0.0)
    with pytest.raises(ValidationError, match="non-positive price"):
        validate(df)


def test_validate_rejects_nan_close():
    """close 含 NaN 拒绝。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": [10.0, float("nan"), 11.0]}, index=idx)
    with pytest.raises(ValidationError, match="non-numeric/NaN price"):
        validate(df)


def test_validate_rejects_non_numeric_close():
    """close 为非数值字符串（coerce 失败）拒绝。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": ["10.0", "abc", "11.0"]}, index=idx)
    with pytest.raises(ValidationError, match="non-numeric/NaN price"):
        validate(df)


def test_validate_rejects_infinite_close():
    """inf 价格绕过 isna 与 <=0，必须拒绝。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": [10.0, float("inf"), 11.0]}, index=idx)
    with pytest.raises(ValidationError, match="non-positive price"):
        validate(df)


def test_validate_checks_sibling_price_columns():
    """存在 close 时同步校验兄弟价格列（high 为负也拒绝）。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": [10.0, 10.0, 10.0], "high": [11.0, -1.0, 11.0]}, index=idx)
    with pytest.raises(ValidationError, match="non-positive price"):
        validate(df)


def test_validate_skips_non_price_metadata_columns():
    """volume/currency/ccy 属元数据列，不参与价格校验。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {
            "close": [10.0, 10.0, 10.0],
            "volume": [0, -1, 5],
            "currency": ["USD", "USD", "USD"],
        },
        index=idx,
    )
    assert validate(df) is None


def test_validate_multi_asset_without_close_column():
    """多资产（无 close 列）逐列校验：任一列非正即拒绝。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"A": [10.0, 10.0, 10.0], "B": [5.0, 0.0, 5.0]}, index=idx)
    with pytest.raises(ValidationError, match="non-positive price"):
        validate(df)


def test_validate_multi_asset_valid_passes():
    """多资产全正价格通过。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"A": [10.0, 11.0, 12.0], "B": [5.0, 6.0, 7.0]}, index=idx)
    assert validate(df) is None


# ---------------------------------------------------------------- 币种校验


def _ccy_frame(values):
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.DataFrame({"close": [10.0] * len(values), "currency": values}, index=idx)


def test_validate_rejects_mixed_currencies():
    """混币种聚合拒绝。"""
    df = _ccy_frame(["USD", "EUR", "USD"])
    with pytest.raises(ValidationError, match="mixed currencies detected"):
        validate(df)


def test_validate_rejects_nan_currency():
    """币种列含 NaN 拒绝（fail-closed）。"""
    df = _ccy_frame(["USD", None, "USD"])
    with pytest.raises(ValidationError, match="NaN currency detected"):
        validate(df)


def test_validate_rejects_currency_mismatch_with_explicit_currency():
    """显式指定币种时要求与数据一致。"""
    df = _ccy_frame(["EUR", "EUR", "EUR"])
    with pytest.raises(ValidationError, match="currency mismatch"):
        validate(df, currency="USD")


def test_validate_accepts_matching_explicit_currency():
    """显式币种与数据一致时通过。"""
    df = _ccy_frame(["USD", "USD", "USD"])
    assert validate(df, currency="USD") is None


def test_validate_detects_ccy_column_case_insensitively():
    """ccy 列与 currency 列同等识别（大小写不敏感）。"""
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": [10.0] * 3, "CCY": ["USD", "CNY", "USD"]}, index=idx)
    with pytest.raises(ValidationError, match="mixed currencies detected"):
        validate(df)
