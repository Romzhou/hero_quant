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
# to skip in multi-asset validation loops. Single source of truth: engine
# imports this set instead of duplicating literals. open/high/low are prices
# (engine._align uses next-day open as executable price) and are validated
# explicitly whenever present — they must NOT be listed here.
NON_PRICE_COLS: frozenset[str] = frozenset({"volume", "currency", "ccy"})

# PIT 旁路二次确认契约字符串（全局冻结）：
# skip_pit=True 或 enforce_pit=False 时，调用方必须显式传入
# pit_ack 与此常量全等（"I_KNOW_THIS_IS_NON_PIT"），否则 BacktestEngine.run 抛 PITViolation。
# 此字符串一经冻结不得修改，任何位置不得另行定义/拼写变体；engine/bench 均复用此单一来源。
PIT_ACK: str = "I_KNOW_THIS_IS_NON_PIT"


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
    # fail-closed: Python already binds the first positionals to the named
    # params, so leftover *args indices no longer align with slots — any extra
    # positional is rejected instead of remapped (remapping misaligns/drops).
    if len(args) > 0:
        raise ValidationError(f"too many positional args (fail-closed): {len(args)} extra")
    # 中文：未知 kwargs fail-closed（防拼写错误关闭校验，如 weights_onn）
    if kwargs:
        raise ValidationError(f"unknown kwargs rejected (fail-closed): {sorted(kwargs)}")

    # 0. 空帧必须显式拒绝 — 禁止空 DataFrame 绕过所有校验
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ValidationError("prices DataFrame is empty or not a DataFrame (fail-closed)")

    # 0a. 非 DatetimeIndex 直接拒绝 — 回测必须有时序索引，否则 pct_change/ret 错位（fail-closed）
    if not isinstance(prices.index, pd.DatetimeIndex):
        raise ValidationError(f"prices index must be DatetimeIndex, got {type(prices.index).__name__} (fail-closed)")

    # 0b. DatetimeIndex 去重校验 — 重复时间戳会导致 pct_change/ret 错位，fail-closed
    if isinstance(prices.index, pd.DatetimeIndex) and prices.index.has_duplicates:
        dup = prices.index[prices.index.duplicated()].unique().tolist()[:5]
        raise ValidationError(f"duplicated timestamps in prices index at {dup} (fail-closed)")

    # 0c. DatetimeIndex 排序校验 — 未按时间递增会导致 pct_change/ret 错位，fail-closed
    if isinstance(prices.index, pd.DatetimeIndex) and not prices.index.is_monotonic_increasing:
        # 已去重，此处未递增即为乱序，拒绝以避免前视/错位收益
        preview = prices.index[:5].tolist()
        raise ValidationError(f"prices index not sorted monotonic increasing: {preview} (fail-closed)")

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
        # Mixed naive/aware PIT inputs: normalize both to UTC-aware instants
        # (naive interpreted as UTC, documented convention) and compare actual
        # instants — the OCR mixed-awareness finding vs the Task13-10 contract
        # (same instant in any representation is NOT a violation).
        # Resolution: normalize-then-compare (keeps Task13-10 green); only a
        # genuinely ambiguous mixed pair — same wall-clock reading where the
        # aware side carries a NON-UTC offset — is fail-closed rejected, since
        # assuming UTC for the naive side could invert the verdict by hours.
        # Same-instant pairs (incl. UTC-aware vs naive) compare exactly.
        try:
            _w_ts_raw = pd.Timestamp(weights_on)
            _p_ts_raw = pd.Timestamp(price_date)
        except (ValueError, TypeError, AttributeError) as e:
            raise ValidationError(f"invalid timestamp: {e}") from e
        _w_aware = _w_ts_raw.tz is not None
        _p_aware = _p_ts_raw.tz is not None
        if _w_aware != _p_aware:
            _aware_raw = _w_ts_raw if _w_aware else _p_ts_raw
            try:
                _off = _aware_raw.utcoffset()
                _off_s = _off.total_seconds() if _off is not None else 0.0
            except (ValueError, TypeError, AttributeError):
                _off_s = 0.0
            _naive_wall = _p_ts_raw if _w_aware else _w_ts_raw
            try:
                _same_wall = _naive_wall == _aware_raw.tz_localize(None)
            except (ValueError, TypeError, AttributeError):
                _same_wall = False
            if _same_wall and _off_s != 0.0:
                raise ValidationError("mixed naive/aware PIT timestamps (fail-closed)")
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

    # 2. 非正价格拒绝：close ≤ 0 视为脏数据；同时 fail-closed on NaN/non-numeric；
    # 有 close 时同步校验兄弟价格列（单 close 分支不跳过 siblings）
    if isinstance(prices, pd.DataFrame) and "close" in prices.columns:
        _price_cols = ["close"] + [c for c in prices.columns if c != "close" and str(c).lower() not in NON_PRICE_COLS]
        for _pc in _price_cols:
            try:
                # 数值化后检查，避免字符串误判；NaN/null/非正/非有限(inf)均拒绝
                _series = pd.to_numeric(prices[_pc], errors="coerce")
                # fail-closed: any NaN (including coercion-introduced), non-positive,
                # or non-finite (+/-inf bypasses both isna and <=0) is dirty
                import numpy as _np

                _arr = _series.to_numpy(dtype=float, na_value=float("nan"))
                if _series.isna().any() or (_series <= 0).any() or (~_np.isfinite(_arr)).any():
                    # 更精确提示：区分 NaN 与非正
                    if _series.isna().any():
                        # 检测是否由非数值 coercion 产生
                        mask = prices[_pc].notna() & _series.isna()
                        bad_idx = mask[mask].index.tolist()[:5]
                        raise ValidationError(
                            f"non-numeric/NaN price detected in prices[{_pc!r}] at {bad_idx} (fail-closed)"
                        )
                    raise ValidationError(f"non-positive price detected in prices[{_pc!r}]")
            except ValidationError:
                raise
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("price validation conversion failed: %s", e, exc_info=True)
                raise ValidationError(f"price validation failed: {e}") from e
    else:
        # multi-asset DataFrame without single "close" column: validate each column as price series
        if isinstance(prices, pd.DataFrame):
            for col in prices.columns:
                # skip non-price metadata columns shared with engine NON_PRICE_COLS（str() 防非字符串列名崩）
                if str(col).lower() in NON_PRICE_COLS:
                    continue
                try:
                    series = pd.to_numeric(prices[col], errors="coerce")
                    import numpy as _np2

                    _arr2 = series.to_numpy(dtype=float, na_value=float("nan"))
                    if series.isna().any() or (series <= 0).any() or (~_np2.isfinite(_arr2)).any():
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
    # case-insensitive currency/ccy lookup matching NON_PRICE_COLS skip semantics
    _ccy_col = next((c for c in prices.columns if str(c).lower() in ("currency", "ccy")), None)
    if isinstance(prices, pd.DataFrame) and _ccy_col is not None:
        try:
            # fail-closed NaN: any NaN currency is invalid (covers both paths consistently)
            if prices[_ccy_col].isna().any():
                bad_idx = prices[prices[_ccy_col].isna()].index.tolist()[:5]
                raise ValidationError(f"NaN currency detected at {bad_idx} (fail-closed)")
            nuniq = prices[_ccy_col].nunique(dropna=False)
            if nuniq > 1:
                raise ValidationError(f"mixed currencies detected: {prices[_ccy_col].unique().tolist()}")
            if currency is not None:
                # 显式指定币种时要求与数据一致
                unique_vals = prices[_ccy_col].dropna().unique()
                if len(unique_vals) > 0 and not (prices[_ccy_col] == currency).all():
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
