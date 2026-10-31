"""回测产物归因与影子账户重放（PR-214）—— 全部来自真实回测输入，零占位值。

## 归因口径（精确、可复核）

输入：`positions`（DataFrame，行=日期索引，列=标的，值为名义持仓金额 h_{t,i}）
与 `prices`（Series，收盘价，索引=日期）。两者按日期 **inner join**，
缺行就少算并计入 warnings（不补值、不外推）。

- `r_t` = prices.pct_change()，与 positions 索引对齐；
  多标的时按等权横截面广播，`r̄_t` = 横截面均值（单价格序列时 r̄_t == r_t）。
- `E_t = Σ_i |h_{t,i}|`（毛敞口），`Ebar = mean(E_t)`。
- `net_return = Σ_t Σ_i h_{t-1,i} · r_{t,i} / Ebar`
  （与 metrics.cumulative_return 口径不同，故额外原样透传
  `metrics_cumulative_return` 供对照，不做强行对齐）。
- `timing = Σ_t (E_{t-1} - Ebar) · r̄_t / Ebar`（仓位偏离均值 × 市场收益）。
- `selection = Σ_t Σ_i h_{t-1,i} · (r_{t,i} - r̄_t) / Ebar`（横截面选股）。
  **列数 < 2（单标的）→ null + not_applicable 追加 "selection_single_symbol"**。
- `cost = -cost_rate · Σ_t Σ_i |Δh_{t,i}| / Ebar`，
  `Δh = positions.diff().fillna(positions.iloc[0])`；
  cost_rate 取 BacktestEngine.run 的 `costs` 默认值（拿不到 → null +
  reason 追加 `cost_rate_unavailable`，**不硬编码 0.0005**）。
- `risk_control = -|metrics.max_drawdown|`（真实风险代价，取自 bundle metrics；
  缺失 → null + `max_drawdown_missing`）。该项**不计入收益分解恒等式**
  （响应 `included_in_identity.risk_control = false`）。
- `residual = net_return - Σ(可算的 timing + selection + cost)`。
- **恒等式**：`Σ(可算的 timing/selection/cost) + residual == net_return`（容差 1e-9）。
- `coverage = 可算类数 / 5`（null 类不计入）。

降级分支（components 全 null + `degraded=True` + 稳定 reason）：
`no_market_data`（sample.bars == 0，静态兜底空壳）/ `positions_missing` /
`prices_missing` / `insufficient_bars`（对齐后 < 3 行）/ `zero_exposure`（Ebar <= 0）。

所有浮点经 `math.isfinite` 校验，NaN/Inf → 该类 null + 对应 reason，
绝不泄漏非有限值，也绝不用 epsilon 占位。
"""

from __future__ import annotations

import inspect
import io
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

ATTRIBUTION_KEYS = ("timing", "selection", "cost", "risk_control", "residual")

# risk_control 是"风险代价"而非收益来源，不进入收益分解恒等式。
INCLUDED_IN_IDENTITY: dict[str, bool] = {
    "timing": True,
    "selection": True,
    "cost": True,
    "risk_control": False,
    "residual": True,
}

DEFAULT_SHADOW_SYMBOL = "600519.SH"

_MIN_BARS = 3  # 少于 3 根对齐行不足以给出可复核的归因（1 根收益 + 均值基准）
_TRADE_EPS = 1e-12  # |Δh| 小于该值视为无成交，不生成影子订单


def _finite(value: Any) -> float | None:
    """转 float 并做有限性校验；非有限/不可转换一律 None（不泄漏 NaN/Inf）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def load_positions(bundle: Any) -> "object | None":
    """取持仓 DataFrame：bundle["positions"] 优先，否则解析 bundle["csv"]（首列 date 为索引）。

    非数值列强制 coerce（不抛错），全空行整行丢弃；解析不出返回 None。
    """
    if not isinstance(bundle, dict):
        return None
    import pandas as pd

    pos = bundle.get("positions")
    if pos is not None and hasattr(pos, "copy") and hasattr(pos, "index"):
        try:
            df = pos.copy()
            if len(list(getattr(df, "columns", []))) == 0:
                return None
            df = df.apply(lambda c: pd.to_numeric(c, errors="coerce"))
            df = df.dropna(how="all")
            if df.empty:
                return None
            return df
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            logger.warning("risk.positions_parse_failed error=%s", e)
            return None
    csv_text = bundle.get("csv")
    if isinstance(csv_text, str) and csv_text.strip():
        try:
            df = pd.read_csv(io.StringIO(csv_text), index_col=0, parse_dates=True)
            if df.empty or len(list(df.columns)) == 0:
                return None
            df = df.apply(lambda c: pd.to_numeric(c, errors="coerce"))
            df = df.dropna(how="all")
            if df.empty:
                return None
            return df
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeDecodeError) as e:
            logger.warning("risk.positions_csv_parse_failed error=%s", e)
            return None
    return None


def load_prices(bundle: Any) -> "object | None":
    """取收盘价 Series：bundle["prices"] 支持 list[{"date","close"}] 或 {date: close}。

    索引统一转 DatetimeIndex 并排序；缺/空/不可解析返回 None。
    """
    if not isinstance(bundle, dict):
        return None
    import pandas as pd

    raw = bundle.get("prices")
    if raw is None:
        return None
    try:
        if isinstance(raw, dict):
            s = pd.Series(raw, dtype="float64")
        elif isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict) and "date" in r and "close" in r]
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.dropna(subset=["date", "close"]).set_index("date").sort_index()
            s = df["close"].astype("float64")
        elif hasattr(raw, "index") and hasattr(raw, "iloc"):
            if hasattr(raw, "columns"):
                cols = list(raw.columns)
                s = raw["close"] if "close" in cols else raw[cols[0]]
            else:
                s = raw
            s = pd.to_numeric(s, errors="coerce").astype("float64")
        else:
            return None
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[s.index.notna()].sort_index()
        s = s.dropna()
        if s.empty:
            return None
        return s
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
        logger.warning("risk.prices_parse_failed error=%s", e)
        return None


def default_cost_rate() -> float | None:
    """成本率取自 BacktestEngine.run 的 costs 默认值；取不到返回 None（绝不硬编码）。"""
    try:
        from hero_quant.backtest.engine import BacktestEngine

        default = inspect.signature(BacktestEngine.run).parameters["costs"].default
        return _finite(default)
    except (ImportError, AttributeError, KeyError, ValueError, TypeError) as e:
        logger.warning("risk.cost_rate_unavailable error=%s", e)
        return None


def _blank_result(reproducibility: Any = None) -> dict:
    return {
        "components": {k: None for k in ATTRIBUTION_KEYS},
        "not_applicable": [],
        "coverage": 0.0,
        "net_return": None,
        "metrics_cumulative_return": None,
        "turnover_notional": None,
        "bars": 0,
        "warnings": [],
        "degraded": True,
        "reason": None,
        "reproducibility": dict(reproducibility) if isinstance(reproducibility, dict) else None,
        "included_in_identity": dict(INCLUDED_IN_IDENTITY),
    }


def compute_attribution(bundle: Any, cost_rate: float | None = None) -> dict:
    """按 docstring 口径计算 5 类归因；不可算的类为 null 并进 not_applicable。"""
    if not isinstance(bundle, dict):
        bundle = {}
    repro = bundle.get("reproducibility")
    out = _blank_result(repro)
    warnings: list[str] = list(bundle.get("warnings") or []) if isinstance(bundle.get("warnings"), list) else []

    metrics = bundle.get("metrics") if isinstance(bundle.get("metrics"), dict) else {}
    out["metrics_cumulative_return"] = _finite(metrics.get("cumulative_return"))

    sample = bundle.get("sample") if isinstance(bundle.get("sample"), dict) else {}
    sample_bars = sample.get("bars")

    def _degrade(reason: str) -> dict:
        out["degraded"] = True
        out["reason"] = reason
        out["warnings"] = warnings
        logger.warning("risk.attribution_degraded reason=%s", reason)
        return out

    # 静态兜底空壳：sample.bars == 0 即"压根没行情"，优先级高于缺字段。
    if sample_bars == 0:
        return _degrade("no_market_data")

    positions = load_positions(bundle)
    if positions is None:
        return _degrade("positions_missing")
    prices = load_prices(bundle)
    if prices is None:
        return _degrade("prices_missing")

    returns = prices.sort_index().pct_change().dropna()
    common = positions.index.intersection(returns.index)
    common = common.sort_values() if hasattr(common, "sort_values") else sorted(common)
    if len(common) < _MIN_BARS:
        out["bars"] = int(len(common))
        return _degrade("insufficient_bars")
    # 首根价格天然没有收益（pct_change 为 NaN），不算对不齐；只有持仓日期在行情里
    # 根本没有对应收盘价才算缺行 —— 缺行就少算并如实披露，不外推不补值。
    missing_price_rows = len(positions.index) - len(positions.index.intersection(prices.index))
    if missing_price_rows > 0:
        warnings.append(f"positions_prices_misaligned missing_price_rows={missing_price_rows}")

    pos = positions.loc[common]
    r_series = returns.loc[common]
    # 首行无"前一日持仓"，从收益归集中剔除（不做 0 持仓假设）。
    usable = common[1:]
    h_prev = pos.shift(1).loc[usable]
    r_u = r_series.loc[usable]
    r_matrix = h_prev.copy()
    for col in r_matrix.columns:
        r_matrix[col] = r_u
    r_bar = r_u  # 单价格序列广播后横截面均值 == r_t 本身

    gross = pos.abs().sum(axis=1)
    e_bar = _finite(gross.mean())
    if e_bar is None or e_bar <= 0:
        return _degrade("zero_exposure")

    net = _finite((h_prev * r_matrix).sum().sum() / e_bar)
    if net is None:
        return _degrade("net_return_not_finite")
    out["net_return"] = net

    components = out["components"]
    not_applicable: list[str] = []

    # timing
    e_prev = gross.shift(1).loc[usable]
    timing = _finite(((e_prev - e_bar) * r_bar).sum() / e_bar)
    if timing is None:
        not_applicable.append("timing_not_finite")
    else:
        components["timing"] = timing

    # selection（单标的无横截面 → 不可算）
    if len(list(pos.columns)) < 2:
        not_applicable.append("selection_single_symbol")
    else:
        dev = r_matrix.sub(r_bar, axis=0)
        selection = _finite((h_prev * dev).sum().sum() / e_bar)
        if selection is None:
            not_applicable.append("selection_not_finite")
        else:
            components["selection"] = selection
            if abs(selection) < 1e-15:
                warnings.append("cross_section_degenerate_single_price_series")

    # cost
    rate = _finite(cost_rate) if cost_rate is not None else default_cost_rate()
    delta = pos.diff()
    delta = delta.fillna(pos.iloc[0])
    turnover = _finite(delta.abs().sum().sum())
    if turnover is None:
        not_applicable.append("turnover_not_finite")
    else:
        out["turnover_notional"] = turnover
        if rate is None:
            not_applicable.append("cost_rate_unavailable")
        else:
            cost = _finite(-rate * turnover / e_bar)
            if cost is None:
                not_applicable.append("cost_not_finite")
            else:
                components["cost"] = cost

    # risk_control（真实风险代价，不进恒等式）
    mdd = _finite(metrics.get("max_drawdown"))
    if mdd is None:
        not_applicable.append("max_drawdown_missing")
    else:
        components["risk_control"] = -abs(mdd)

    # residual 吸收恒等式残差
    summable = [components[k] for k in ("timing", "selection", "cost") if components[k] is not None]
    residual = _finite(net - sum(summable))
    if residual is None:
        not_applicable.append("residual_not_finite")
    else:
        components["residual"] = residual

    covered = sum(1 for k in ATTRIBUTION_KEYS if components[k] is not None)
    out["coverage"] = covered / len(ATTRIBUTION_KEYS)
    out["not_applicable"] = not_applicable
    out["bars"] = int(len(usable))
    out["warnings"] = warnings
    out["degraded"] = covered < len(ATTRIBUTION_KEYS)
    out["reason"] = ";".join(not_applicable) if not_applicable else None
    if out["degraded"]:
        logger.warning("risk.attribution_partial reason=%s coverage=%s", out["reason"], out["coverage"])
    return out


def build_shadow_replay(bundle: Any) -> dict:
    """用真实回测 fills（positions.diff()，首行取首值）重放为影子订单，逐笔过真实风控规则。

    不调用 ShadowJournal.attribution()（那是 5 类各 0.01 的 epsilon 占位）。
    """
    if not isinstance(bundle, dict):
        bundle = {}
    repro = bundle.get("reproducibility")
    out: dict[str, Any] = {
        "orders_total": None,
        "allowed": None,
        "rejected": None,
        "rejected_by_rule": {},
        "reject_rate": None,
        "notional_total": None,
        "turnover_notional": None,
        "turnover_mean_exposure": None,
        "bars": 0,
        "warnings": [],
        "degraded": True,
        "reason": None,
        "reproducibility": dict(repro) if isinstance(repro, dict) else None,
    }
    warnings: list[str] = []

    def _degrade(reason: str) -> dict:
        out["degraded"] = True
        out["reason"] = reason
        out["warnings"] = warnings
        logger.warning("risk.shadow_degraded reason=%s", reason)
        return out

    positions = load_positions(bundle)
    if positions is None or len(positions.index) < 2:
        return _degrade("no_fills")
    prices = load_prices(bundle)
    if prices is None:
        return _degrade("prices_missing")

    out["bars"] = int(len(positions.index))
    px = prices.reindex(positions.index)
    delta = positions.diff().fillna(positions.iloc[0])

    try:
        from hero_quant.shadow.service import RiskEngine

        engine = RiskEngine()
    except (ImportError, AttributeError, ValueError, TypeError) as e:
        logger.warning("risk.shadow_engine_unavailable error=%s", e)
        return _degrade("risk_engine_unavailable")

    orders_total = 0
    allowed = 0
    rejected_by_rule: dict[str, int] = {}
    notional_total = 0.0
    price_missing = 0

    for ts, row in delta.iterrows():
        price = _finite(px.loc[ts]) if ts in px.index else None
        for col in delta.columns:
            notional = _finite(row[col])
            if notional is None or abs(notional) < _TRADE_EPS:
                continue  # 无成交不生成订单，避免用 0 量刷拒单率
            if price is None or price <= 0:
                price_missing += 1
                continue  # 无真实价格不臆造 qty
            qty = abs(notional) / price
            order = {
                "symbol": str(col) if str(col).strip() else DEFAULT_SHADOW_SYMBOL,
                "qty": qty,
                "price": price,
                "side": "buy" if notional > 0 else "sell",
            }
            orders_total += 1
            notional_total += abs(qty * price)
            verdict = engine.check_order(order)
            if bool(verdict.get("allowed")):
                allowed += 1
            else:
                rule = str(verdict.get("rule") or "unknown")
                rejected_by_rule[rule] = rejected_by_rule.get(rule, 0) + 1

    turnover = _finite(delta.abs().sum().sum())
    gross = positions.abs().sum(axis=1)
    e_bar = _finite(gross.mean())
    out["turnover_notional"] = turnover
    out["turnover_mean_exposure"] = (turnover / e_bar) if (turnover is not None and e_bar not in (None, 0)) else None
    if price_missing:
        warnings.append(f"price_missing_bars={price_missing}")
    out["warnings"] = warnings

    if orders_total == 0:
        return _degrade("no_fills")

    rejected = orders_total - allowed
    out["orders_total"] = orders_total
    out["allowed"] = allowed
    out["rejected"] = rejected
    out["rejected_by_rule"] = rejected_by_rule
    out["notional_total"] = notional_total
    out["reject_rate"] = rejected / orders_total
    out["degraded"] = False
    out["reason"] = None
    return out
