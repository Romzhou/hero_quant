"""回测现实性（PR-234）：交易成本、滑点、涨跌停、停牌/退市与买入持有基准。

职责：提供可显式启用的现实性原语与汇总报告，补足 BacktestEngine 默认不做摩擦建模的空白。
架构位置：backtest 层的纯函数工具，不进入 BacktestEngine.run 的既有路径（默认行为零变化）。
关键设计：bps = 万分之一（1 bps = 1e-4）；一切缺数据的路径返回 degraded=True + 稳定 reason，
其余数值字段一律 None，不补零、不猜价；基准回撤与 metrics.max_drawdown 同口径。
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .metrics import max_drawdown

logger = logging.getLogger(__name__)

__all__ = [
    "TradingCosts",
    "TradingConstraints",
    "apply_slippage",
    "apply_price_limit",
    "is_halted",
    "delisted_after",
    "buy_and_hold",
    "realism_report",
]

_BPS = 1e4
_BUY_SIDES = frozenset({"buy", "b", "long", "买"})
_SELL_SIDES = frozenset({"sell", "s", "short", "卖"})
_DELISTED_FLAGS = ("delisted", "is_delisted")
_DELISTED_STATUS = frozenset({"delisted", "退市", "terminated"})
_PRICE_KEYS = ("price", "fill_price", "avg_price", "exec_price")
_QTY_KEYS = ("qty", "quantity", "shares", "size")
_NOTIONAL_KEYS = ("notional", "amount", "value")
_PREV_CLOSE_KEYS = ("prev_close", "previous_close", "prevclose")
_SYMBOL_KEYS = ("symbol", "ticker")
_DATE_KEYS = ("trade_date", "date", "ts")
_NUMERIC_KEYS = (
    "trades",
    "notional",
    "commission",
    "slippage",
    "stamp_duty",
    "total_cost",
    "cost_bps",
    "halted_bars",
    "clipped_fills",
)


# --------------------------------------------------------------------- 数值守卫

def _finite_float(value: Any) -> float | None:
    """宽进严出：可转 float 且有限才返回，否则 None（不把 NaN/Inf/脏字符串带进模型）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _require_finite(value: Any, name: str) -> float:
    out = _finite_float(value)
    if out is None:
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return out


def _require_positive(value: Any, name: str) -> float:
    out = _require_finite(value, name)
    if out <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return out


def _require_non_negative(value: Any, name: str) -> float:
    out = _require_finite(value, name)
    if out < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return out


def _normalize_side(side: Any) -> str:
    """归一化买卖方向：buy/b/long/买 -> buy，sell/s/short/卖 -> sell，其余 ValueError。"""
    text = str(side).strip().lower()
    if text in _BUY_SIDES:
        return "buy"
    if text in _SELL_SIDES:
        return "sell"
    raise ValueError(f"side must be buy/sell (aliases: long/short), got {side!r}")


def _present(value: Any) -> bool:
    """判断字段是否真的给了值：None/NaN/NaT 视为缺失，其余（含 0）视为存在。"""
    if value is None:
        return False
    try:
        return not bool(pd.isna(value))
    except (TypeError, ValueError):
        return True


def _first(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and _present(record[key]):
            return record[key]
    return None


def _as_date(value: Any) -> _dt.date | None:
    """尽力解析成 date；解析不出来返回 None（不猜日期）。"""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- 成本/约束

@dataclass(frozen=True)
class TradingCosts:
    """交易成本，单位 bps（万分之一）。

    - commission_bps: 双边佣金
    - slippage_bps: 双边滑点（成本近似 = 成交名义额 * bps / 1e4）
    - stamp_duty_bps: 印花税，仅卖出收取（A 股现行 5bps）
    """

    commission_bps: float = 3.0
    slippage_bps: float = 2.0
    stamp_duty_bps: float = 5.0

    def __post_init__(self) -> None:
        for name in ("commission_bps", "slippage_bps", "stamp_duty_bps"):
            _require_non_negative(getattr(self, name), name)


@dataclass(frozen=True)
class TradingConstraints:
    """交易约束；price_limit_pct=None 表示不限制（默认保持既有行为）。"""

    price_limit_pct: float | None = None

    def __post_init__(self) -> None:
        if self.price_limit_pct is None:
            return
        pct = _require_non_negative(self.price_limit_pct, "price_limit_pct")
        if pct >= 1.0:
            raise ValueError(f"price_limit_pct must be < 1.0, got {self.price_limit_pct!r}")


def apply_slippage(price: float, side: str, *, bps: float) -> float:
    """按 bps（万分之一）返回含滑点的成交价。

    买入抬价：price * (1 + bps/1e4)；卖出压价：price * (1 - bps/1e4)。
    bps=0 原价返回；价格非正/非有限、bps 为负、方向无法识别均抛 ValueError（fail-closed）。
    """
    px = _require_positive(price, "price")
    cost = _require_non_negative(bps, "bps")
    direction = _normalize_side(side)
    ratio = cost / _BPS
    adjusted = px * (1.0 + ratio) if direction == "buy" else px * (1.0 - ratio)
    if not math.isfinite(adjusted) or adjusted <= 0:
        raise ValueError(f"slippage {bps!r} bps drives price non-positive: {adjusted!r}")
    return float(adjusted)


def apply_price_limit(prev_close: float, target: float, *, limit_pct: float | None) -> float:
    """把目标价裁剪到涨跌停区间 [prev_close*(1-limit_pct), prev_close*(1+limit_pct)]。

    limit_pct=None 时原样返回 target（不限制）；突破上限/下限时裁剪到限价，区间内原值返回。
    价格非正、target 非有限、limit_pct 为负或 >=1 均抛 ValueError。
    """
    prev = _require_positive(prev_close, "prev_close")
    target_px = _require_finite(target, "target")
    if limit_pct is None:
        return float(target_px)
    pct = _require_non_negative(limit_pct, "limit_pct")
    if pct >= 1.0:
        raise ValueError(f"limit_pct must be < 1.0, got {limit_pct!r}")
    lower = prev * (1.0 - pct)
    upper = prev * (1.0 + pct)
    return float(min(max(target_px, lower), upper))


# --------------------------------------------------------------------- 停牌/退市

def is_halted(bar: Mapping[str, Any]) -> bool:
    """volume == 0 视为停牌。

    volume 缺失 / 非数值 / NaN 时返回 False：缺量不能当停牌猜，由 realism_report 判
    reason="volume_missing" 并整体降级。
    """
    if not isinstance(bar, Mapping):
        raise TypeError("bar must be a mapping")
    volume = _finite_float(bar.get("volume"))
    return volume is not None and volume == 0.0


def delisted_after(bars: Any) -> _dt.date | None:
    """返回首个显式退市标记的交易日；无标记返回 None（未知，不猜）。

    识别字段（任一）：
      - bar["delist_date"]：直接取该日期
      - bar["delisted"] / bar["is_delisted"] 为真：取该 bar 的 trade_date
      - bar["status"] in {"delisted", "退市", "terminated"}：取该 bar 的 trade_date
    多个标记取最早日期；空 bars / 无可解析日期返回 None。
    """
    rows = _bar_rows(bars)
    candidates: list[_dt.date] = []
    for bar in rows:
        explicit = _as_date(_first(bar, ("delist_date",)))
        if explicit is not None:
            candidates.append(explicit)
            continue
        flagged = any(bool(bar.get(key)) for key in _DELISTED_FLAGS if key in bar)
        status = str(bar.get("status", "")).strip().lower()
        if flagged or status in _DELISTED_STATUS:
            bar_date = _as_date(_first(bar, _DATE_KEYS))
            if bar_date is not None:
                candidates.append(bar_date)
    return min(candidates) if candidates else None


# --------------------------------------------------------------------- 基准

def _degraded_benchmark(reason: str) -> dict[str, Any]:
    logger.warning("buy_and_hold degraded error_code=%s", reason)
    return {
        "total_return": None,
        "max_drawdown": None,
        "series": None,
        "degraded": True,
        "reason": reason,
    }


def buy_and_hold(prices: pd.Series) -> dict[str, Any]:
    """买入持有基准：净值序列 series = price / price[0]，total_return = series[-1]-1。

    max_drawdown 直接复用 metrics.max_drawdown（负值口径）。缺/脏数据一律降级为 None + reason：
      - prices_missing：None / 空序列
      - price_missing：存在 NaN 或非数值
      - price_non_positive：存在 <= 0 或非有限价格
    单点序列是合法输入（收益 0、回撤 0），不视为降级。
    """
    if prices is None:
        return _degraded_benchmark("prices_missing")
    if isinstance(prices, pd.DataFrame):
        raise TypeError("buy_and_hold expects a pandas Series, not DataFrame")
    series_in = prices if isinstance(prices, pd.Series) else pd.Series(prices)
    if series_in.empty:
        return _degraded_benchmark("prices_missing")
    numeric = pd.to_numeric(series_in, errors="coerce")
    if numeric.isna().any():
        return _degraded_benchmark("price_missing")
    values = numeric.to_numpy(dtype=float)
    if not np.all(np.isfinite(values)):
        return _degraded_benchmark("price_missing")
    if np.any(values <= 0):
        return _degraded_benchmark("price_non_positive")
    equity = pd.Series(values / values[0], index=series_in.index)
    return {
        "total_return": float(equity.iloc[-1] - 1.0),
        "max_drawdown": float(max_drawdown(equity)),
        "series": equity,
        "degraded": False,
        "reason": None,
    }


# --------------------------------------------------------------------- bars 规整

def _bar_rows(bars: Any) -> list[Mapping[str, Any]]:
    if bars is None:
        return []
    if isinstance(bars, pd.DataFrame):
        frame = bars
        if "trade_date" not in frame.columns:
            frame = frame.reset_index().rename(columns={"index": "trade_date"})
        return [row for row in frame.to_dict("records") if isinstance(row, Mapping)]
    try:
        rows = list(bars)
    except TypeError:
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _bar_volume(bar: Mapping[str, Any]) -> float | None:
    return _finite_float(bar.get("volume"))


def _bar_close(bar: Mapping[str, Any]) -> float | None:
    close = _finite_float(bar.get("close"))
    return close if close is not None and close > 0 else None


def _bar_symbol(bar: Mapping[str, Any]) -> str:
    symbol = _first(bar, _SYMBOL_KEYS)
    return "" if symbol is None else str(symbol)


def _ordered_bars(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """全部 bar 都有可解析交易日时按日期升序（同日保序），否则保持入参顺序。"""
    stamp = [_as_date(_first(row, _DATE_KEYS)) for row in rows]
    if rows and all(item is not None for item in stamp):
        order = sorted(range(len(rows)), key=lambda i: (stamp[i], i))
        return [rows[i] for i in order]
    return list(rows)


# --------------------------------------------------------------------- fills 规整

def _fill_rows(fills: Any) -> list[Mapping[str, Any]]:
    if isinstance(fills, pd.DataFrame):
        return [row for row in fills.reset_index(drop=True).to_dict("records") if isinstance(row, Mapping)]
    try:
        rows = list(fills)
    except TypeError as exc:
        raise ValueError(f"fills must be a sequence of mappings or DataFrame, got {type(fills).__name__}") from exc
    return rows


def _normalize_fill(record: Any) -> dict[str, Any]:
    """把一条成交规整成 {price,qty,notional,side,prev_close,symbol,trade_date}，缺必需字段抛 ValueError。"""
    if not isinstance(record, Mapping):
        raise ValueError(f"fill must be a mapping, got {type(record).__name__}")

    price_raw = _first(record, _PRICE_KEYS)
    price = None if price_raw is None else _finite_float(price_raw)
    if price_raw is not None and (price is None or price <= 0):
        raise ValueError(f"fill price must be positive finite, got {price_raw!r}")

    qty_raw = _first(record, _QTY_KEYS)
    qty = None if qty_raw is None else _finite_float(qty_raw)
    if qty_raw is not None and qty is None:
        raise ValueError(f"fill qty must be finite, got {qty_raw!r}")

    notional_raw = _first(record, _NOTIONAL_KEYS)
    notional = None
    if notional_raw is not None:
        parsed_notional = _finite_float(notional_raw)
        if parsed_notional is None:
            raise ValueError(f"fill notional must be finite, got {notional_raw!r}")
        notional = abs(parsed_notional)

    if price is None and notional is None:
        raise ValueError("fill needs price or notional")

    side_raw = _first(record, ("side", "direction"))
    if side_raw is not None:
        side = _normalize_side(side_raw)
    elif qty is not None and qty != 0:
        side = "sell" if qty < 0 else "buy"
    else:
        raise ValueError("fill needs side or signed qty (stamp duty depends on side)")

    prev_raw = _first(record, _PREV_CLOSE_KEYS)
    prev_close = None if prev_raw is None else _finite_float(prev_raw)
    if prev_raw is not None and (prev_close is None or prev_close <= 0):
        raise ValueError(f"fill prev_close must be positive finite, got {prev_raw!r}")

    symbol_raw = _first(record, _SYMBOL_KEYS)
    return {
        "price": price,
        "qty": qty,
        "notional": notional,
        "side": side,
        "prev_close": prev_close,
        "symbol": None if symbol_raw is None else str(symbol_raw),
        "trade_date": _as_date(_first(record, _DATE_KEYS)),
    }


def _prev_close_table(rows: list[Mapping[str, Any]]) -> dict[str, list[tuple[_dt.date, float]]]:
    table: dict[str, list[tuple[_dt.date, float]]] = {}
    for bar in rows:
        trade_date = _as_date(_first(bar, _DATE_KEYS))
        close = _bar_close(bar)
        if trade_date is None or close is None:
            continue
        table.setdefault(_bar_symbol(bar), []).append((trade_date, close))
    for series in table.values():
        series.sort(key=lambda item: item[0])
    return table


def _lookup_prev_close(
    table: dict[str, list[tuple[_dt.date, float]]],
    symbol: str | None,
    trade_date: _dt.date | None,
) -> float | None:
    """取该标的在 trade_date 之前最近一根 bar 的收盘价；单标的 bars 允许按唯一 symbol 兜底。"""
    if trade_date is None or not table:
        return None
    rows = table.get("" if symbol is None else str(symbol))
    if rows is None and len(table) == 1:
        rows = next(iter(table.values()))
    if not rows:
        return None
    previous: float | None = None
    for bar_date, close in rows:
        if bar_date >= trade_date:
            break
        previous = close
    return previous


def _fill_notional(fill: dict[str, Any]) -> float:
    """成交名义额：有价有量按裁剪后价格重算，否则用调用方给的 notional（已取绝对值）。"""
    if fill["price"] is not None and fill["qty"] is not None:
        return abs(float(fill["qty"]) * float(fill["price"]))
    return float(fill["notional"])


# --------------------------------------------------------------------- 汇总报告

def _degraded_report(reason: str, *, delisted_at: str | None = None) -> dict[str, Any]:
    logger.warning("backtest realism degraded error_code=%s", reason)
    report: dict[str, Any] = {key: None for key in _NUMERIC_KEYS}
    report["degraded"] = True
    report["reason"] = reason
    report["delisted_at"] = delisted_at
    return report


def realism_report(
    *,
    fills: Any,
    bars: Any,
    costs: TradingCosts,
    constraints: TradingConstraints,
) -> dict[str, Any]:
    """汇总现实性报告：成本拆分、停牌 bar 数、涨跌停裁剪次数、退市日。

    输入：
      - bars: Sequence[Mapping] 或 DataFrame，需含 volume；带 symbol/trade_date/close 时可用于
        按「前一交易日收盘」推导 prev_close。
      - fills: Sequence[Mapping] 或 DataFrame，字段别名见 _PRICE_KEYS 等；side 可由带符号 qty 推出。
      - costs / constraints: 显式传入，默认 TradingCosts() / TradingConstraints() 不在此隐式兜底。

    输出键：degraded / reason / trades / notional / commission / slippage / stamp_duty /
    total_cost / cost_bps / halted_bars / clipped_fills / delisted_at。

    降级优先级（首次命中即返回，数值全 None，不猜不补零）：
      bars_missing -> volume_missing -> all_halted -> delisted_unknown ->
      fills_missing -> fills_invalid -> prev_close_missing
    delisted_unknown = 末根 bar 停牌且无显式退市标记：分不清「停牌」还是「退市」，
    退出价不可信，故整体降级。
    """
    if not isinstance(costs, TradingCosts):
        raise TypeError(f"costs must be TradingCosts, got {type(costs).__name__}")
    if not isinstance(constraints, TradingConstraints):
        raise TypeError(f"constraints must be TradingConstraints, got {type(constraints).__name__}")

    rows = _ordered_bars(_bar_rows(bars))
    if not rows:
        return _degraded_report("bars_missing")
    if any(_bar_volume(bar) is None for bar in rows):
        return _degraded_report("volume_missing")

    halted_bars = sum(1 for bar in rows if is_halted(bar))
    delisted = delisted_after(rows)
    delisted_at = delisted.isoformat() if delisted is not None else None
    if halted_bars == len(rows):
        return _degraded_report("all_halted", delisted_at=delisted_at)
    if delisted is None and is_halted(rows[-1]):
        return _degraded_report("delisted_unknown")

    if fills is None:
        return _degraded_report("fills_missing", delisted_at=delisted_at)
    try:
        records = _fill_rows(fills)
        normalized = [_normalize_fill(record) for record in records]
    except ValueError as exc:
        logger.warning("backtest realism fills rejected error_code=fills_invalid detail=%s", exc)
        return _degraded_report("fills_invalid", delisted_at=delisted_at)

    limit_pct = constraints.price_limit_pct
    if limit_pct is not None:
        if any(fill["price"] is None for fill in normalized):
            return _degraded_report("fills_invalid", delisted_at=delisted_at)
        table = _prev_close_table(rows)
        for fill in normalized:
            if fill["prev_close"] is None:
                fill["prev_close"] = _lookup_prev_close(table, fill["symbol"], fill["trade_date"])
        if any(fill["prev_close"] is None for fill in normalized):
            return _degraded_report("prev_close_missing", delisted_at=delisted_at)

    commission = 0.0
    slippage = 0.0
    stamp_duty = 0.0
    notional_total = 0.0
    clipped_fills = 0
    for fill in normalized:
        if limit_pct is not None and fill["price"] is not None:
            raw_price = float(fill["price"])
            limited = apply_price_limit(float(fill["prev_close"]), raw_price, limit_pct=limit_pct)
            if not math.isclose(limited, raw_price, rel_tol=1e-12, abs_tol=1e-12):
                clipped_fills += 1
            fill["price"] = limited
        notional = _fill_notional(fill)
        notional_total += notional
        commission += notional * costs.commission_bps / _BPS
        slippage += notional * costs.slippage_bps / _BPS
        if fill["side"] == "sell":
            stamp_duty += notional * costs.stamp_duty_bps / _BPS

    total_cost = commission + slippage + stamp_duty
    cost_bps = (total_cost / notional_total * _BPS) if notional_total > 0 else 0.0
    return {
        "degraded": False,
        "reason": None,
        "trades": len(normalized),
        "notional": float(notional_total),
        "commission": float(commission),
        "slippage": float(slippage),
        "stamp_duty": float(stamp_duty),
        "total_cost": float(total_cost),
        "cost_bps": float(cost_bps),
        "halted_bars": int(halted_bars),
        "clipped_fills": int(clipped_fills),
        "delisted_at": delisted_at,
    }
