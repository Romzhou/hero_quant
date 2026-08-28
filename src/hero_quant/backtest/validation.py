"""回测校验：PIT 时序、价格有效性与币种一致性。

职责：为 BacktestEngine 提供前置校验，阻断未来数据与脏价格进入回测。
架构位置：engine.run 入口的可选校验层，亦可独立调用；PIT 失败直接抛 ValidationError。
关键设计：PIT 正逻辑 weights_on ≤ price_date（ts_w > ts_p 视为使用未来数据）；非正价格拒绝；混币种聚合拒绝。

PIT: weights_on must be <= price_date — weights generated on weights_on may only use
price_date that is on or after weights_on; if weights_on > price_date the weights
would require future prices and must be rejected.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """输入违反 PIT/价格/币种任一正确性约束时抛出。"""


def validate(
    prices: pd.DataFrame,
    weights_on: str | pd.Timestamp | None = None,
    price_date: str | pd.Timestamp | None = None,
    currency: str | None = None,
    *args,
    **kwargs,
) -> None:
    """校验回测输入：PIT 时序、非正价格与混币种；通过则返回 None，违规抛 ValidationError。

    PIT: weights_on must be <= price_date.
        - weights_on <= price_date : valid (weights use data available at or before price_date)
        - weights_on > price_date  : invalid (weights would need future data) -> raise ValidationError
    """
    # 兼容：允许经 kwargs/*args 传入同名参数
    if weights_on is None and "weights_on" in kwargs:
        weights_on = kwargs.pop("weights_on")
    if price_date is None and "price_date" in kwargs:
        price_date = kwargs.pop("price_date")
    if currency is None and "currency" in kwargs:
        currency = kwargs.pop("currency")

    # 兼容位置参数 validate(prices, weights_on, price_date, currency)
    if weights_on is None and len(args) >= 1:
        weights_on = args[0]
    if price_date is None and len(args) >= 2:
        price_date = args[1]
    if currency is None and len(args) >= 3:
        currency = args[2]

    # 1. PIT 校验：weights_on ≤ price_date 为正逻辑
    if weights_on is not None and price_date is not None:
        try:
            ts_w = pd.Timestamp(weights_on)
            ts_p = pd.Timestamp(price_date)
        except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime) as e:
            raise ValidationError(f"invalid date format: {e}") from e
        # 使用未来数据直接拒绝：仅当 ts_w > ts_p 时违规
        if ts_w > ts_p:
            raise ValidationError(
                f"PIT violation: weights_on {ts_w.date()} > price_date {ts_p.date()} uses future data"
            )

    # 2. 非正价格拒绝：close ≤ 0 视为脏数据
    if isinstance(prices, pd.DataFrame) and "close" in prices.columns:
        try:
            # 数值化后检查，避免字符串误判
            close = pd.to_numeric(prices["close"], errors="coerce")
            if (close <= 0).any():
                raise ValidationError("non-positive price detected in prices['close']")
        except ValidationError:
            raise
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("price validation conversion failed: %s", e)

    # 3. 混币种聚合拒绝
    if isinstance(prices, pd.DataFrame) and "currency" in prices.columns:
        try:
            nuniq = prices["currency"].nunique(dropna=False)
            if nuniq > 1:
                raise ValidationError(f"mixed currencies detected: {prices['currency'].unique().tolist()}")
            if currency is not None:
                # 显式指定币种时要求与数据一致
                unique_vals = prices["currency"].dropna().unique()
                if len(unique_vals) > 0 and not (prices["currency"] == currency).all():
                    raise ValidationError(
                        f"currency mismatch: expected {currency}, got {unique_vals.tolist()}"
                    )
        except ValidationError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            logger.warning("currency validation failed: %s", e)

    # 4. 字符串日期已在 PIT 步骤经 pd.Timestamp 解析

    return None
