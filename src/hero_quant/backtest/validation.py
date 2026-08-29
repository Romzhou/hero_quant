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

# Shared with BacktestEngine._price_matrix / _align — non-price metadata columns
# to skip in multi-asset validation loops. Keep in sync with engine.
NON_PRICE_COLS: frozenset[str] = frozenset({"open", "high", "low", "volume", "currency", "ccy"})


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

    # 0. 空帧必须显式拒绝 — 禁止空 DataFrame 绕过所有校验
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ValidationError("prices DataFrame is empty or not a DataFrame (fail-closed)")

    # 0b. Duplicate DatetimeIndex check — duplicated timestamps would corrupt pct_change/ret alignment
    if isinstance(prices.index, pd.DatetimeIndex) and prices.index.has_duplicates:
        dup = prices.index[prices.index.duplicated()].unique().tolist()[:5]
        raise ValidationError(f"duplicated timestamps in prices index at {dup} (fail-closed)")

    # 1. PIT 校验：weights_on ≤ price_date 为正逻辑
    # Normalize TZ-aware vs naive to UTC consistently before comparison
    def _norm_ts(v):
        ts = pd.Timestamp(v)
        try:
            if ts.tz is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
        except (TypeError, ValueError, AttributeError) as e:
            raise ValidationError(f"invalid timestamp {v!r}: {e}") from e
        return ts

    if weights_on is not None and price_date is not None:
        try:
            ts_w = _norm_ts(weights_on)
            ts_p = _norm_ts(price_date)
        except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime) as e:
            raise ValidationError(f"invalid date format: {e}") from e
        # 使用未来数据直接拒绝：仅当 ts_w > ts_p 时违规
        if ts_w > ts_p:
            raise ValidationError(
                f"PIT violation: weights_on {ts_w.date()} > price_date {ts_p.date()} uses future data"
            )

    # 2. 非正价格拒绝：close ≤ 0 视为脏数据；同时 fail-closed on NaN/non-numeric
    if isinstance(prices, pd.DataFrame) and "close" in prices.columns:
        try:
            # 数值化后检查，避免字符串误判；NaN/null 视为脏数据直接拒绝
            close = pd.to_numeric(prices["close"], errors="coerce")
            # fail-closed: any NaN (including coercion-introduced) 或非正均拒绝
            if close.isna().any() or (close <= 0).any():
                # 更精确提示：区分 NaN 与非正
                if close.isna().any():
                    # 检测是否由非数值 coercion 产生
                    mask = prices["close"].notna() & close.isna()
                    bad_idx = mask[mask].index.tolist()[:5]
                    raise ValidationError(
                        f"non-numeric/NaN price detected in prices['close'] at {bad_idx} (fail-closed)"
                    )
                raise ValidationError("non-positive price detected in prices['close']")
        except ValidationError:
            raise
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("price validation conversion failed: %s", e, exc_info=True)
            raise ValidationError(f"price validation failed: {e}") from e
    else:
        # multi-asset DataFrame without single "close" column: validate each column as price series
        if isinstance(prices, pd.DataFrame):
            for col in prices.columns:
                # skip non-price metadata columns shared with engine NON_PRICE_COLS
                if col.lower() in NON_PRICE_COLS:
                    continue
                try:
                    series = pd.to_numeric(prices[col], errors="coerce")
                    if series.isna().any() or (series <= 0).any():
                        if series.isna().any():
                            mask = prices[col].notna() & series.isna()
                            bad_idx = mask[mask].index.tolist()[:5]
                            raise ValidationError(
                                f"non-numeric/NaN price detected in prices[{col!r}] at {bad_idx} (fail-closed)"
                            )
                        raise ValidationError(f"non-positive price detected in prices[{col!r}]")
                except ValidationError:
                    raise
                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning("price validation conversion failed for column %r: %s", col, e, exc_info=True)
                    raise ValidationError(f"price validation failed for column {col!r}: {e}") from e

    # 3. 混币种聚合拒绝 — 一致 NaN 策略：NaN 视为无效，fail-closed
    if isinstance(prices, pd.DataFrame) and "currency" in prices.columns:
        try:
            # fail-closed NaN: any NaN currency is invalid (covers both paths consistently)
            if prices["currency"].isna().any():
                bad_idx = prices[prices["currency"].isna()].index.tolist()[:5]
                raise ValidationError(f"NaN currency detected at {bad_idx} (fail-closed)")
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
            logger.warning("currency validation failed: %s", e, exc_info=True)
            raise ValidationError(f"currency validation failed: {e}") from e

    # 4. 字符串日期已在 PIT 步骤经 pd.Timestamp 解析

    return None
