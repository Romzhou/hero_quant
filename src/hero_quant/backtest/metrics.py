"""绩效指标：纯 pandas/numpy 的回测后验计算。

职责：基于权益曲线计算 sharpe、max_drawdown、annual_return、turnover 等，并汇总为 compute_metrics。
架构位置：被 BacktestEngine 调用，产出 tearsheet 所需指标；不依赖外部量化库。
关键设计：年化以 252 交易日为基准；除零/空数据/NaN 均回落为 0，避免指标发散。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sharpe_ratio(equity: pd.Series, risk_free: float = 0.0, periods: int = 252) -> float:
    """年化 Sharpe：(日超额均值 / 日波动) * sqrt(252)，空/零波动回落 0。"""
    import math

    if equity is None or len(equity) < 2:
        return 0.0
    # 日收益序列 — sanitize ±inf (e.g. equity hitting 0 then recovering yields
    # inf pct_change) before std/mean, else Sharpe is garbage
    ret = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if ret.empty:
        return 0.0
    try:
        _std = float(ret.std(ddof=1))
    except (ValueError, TypeError):
        return 0.0
    # 中文：容差零判断 + NaN/Inf 不安全比较一律回落（1e-13 级抖动视为零波动）
    if not np.isfinite(_std) or math.isclose(_std, 0.0, abs_tol=1e-9):
        return 0.0  # 零波动或无效数据无法定义 Sharpe
    # 年化无风险折为日
    rf_daily = risk_free / periods
    excess = ret - rf_daily  # 超额收益
    sr = excess.mean() / excess.std(ddof=1) * np.sqrt(periods)
    if np.isnan(sr) or np.isinf(sr):
        return 0.0
    return float(sr)


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤（负值，如 -0.05）：min(equity / cummax - 1)，空序列回落 0。"""
    if equity is None or len(equity) == 0:
        return 0.0
    # 兼容单列 DataFrame 传入 — DataFrame check must come before pd.Series()
    # coercion, else multi-column input raises before reaching iloc[:, 0]
    if isinstance(equity, pd.DataFrame):
        s = equity.iloc[:, 0]
    elif isinstance(equity, pd.Series):
        s = equity
    else:
        s = pd.Series(equity)
    s = pd.to_numeric(s, errors="coerce")
    cummax = s.cummax()  # 滚动峰值
    # 避免除零：cummax==0 处回撤定义为 0，不用 inf/NaN 掩盖后续真实回撤
    dd = s / cummax - 1.0
    dd = dd.where(cummax != 0, 0.0)
    dd = dd.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # 最深回撤（最小值即最负）
    mdd = float(dd.min()) if not dd.empty else 0.0
    if np.isnan(mdd) or np.isinf(mdd):
        return 0.0
    return mdd


def annual_return(equity: pd.Series, periods: int = 252) -> float:
    """年化收益（CAGR）：(end/start)^(252/n)-1，起点为 0 或空回落 0。"""
    import math

    if equity is None or len(equity) < 2:
        return 0.0
    # DataFrame check before pd.Series() coercion (see max_drawdown)
    if isinstance(equity, pd.DataFrame):
        s = equity.iloc[:, 0]
    elif isinstance(equity, pd.Series):
        s = equity
    else:
        s = pd.Series(equity)
    # 中文：头部数值化，脏数据（字符串/None）coerce 后 dropna，空则回落 0 不抛错
    try:
        s = pd.to_numeric(s, errors="coerce").dropna()
    except (ValueError, TypeError, AttributeError):
        return 0.0
    if len(s) < 2:
        return 0.0
    # CAGR 时间基：用首尾有效观测在原序列中的跨度（含 NaN 缺口），不用 dropna
    # 后长度 — 内部缺口也消耗真实时间，不应缩短年化基数
    try:
        _valid_mask = pd.to_numeric(
            equity.iloc[:, 0] if isinstance(equity, pd.DataFrame) else equity,
            errors="coerce",
        ).notna()
        _pos = _valid_mask[_valid_mask].index
        n = len(s) - 1
        if len(_pos) >= 2:
            try:
                _i0 = _valid_mask.tolist().index(True)
                _i1 = len(_valid_mask) - 1 - _valid_mask.tolist()[::-1].index(True)
                n = max(_i1 - _i0, 1)
            except (ValueError, TypeError, AttributeError):
                pass
    except (ValueError, TypeError, AttributeError):
        n = len(s) - 1
    if n <= 0:
        return 0.0
    try:
        start = float(s.iloc[0])  # 起点净值
        end = float(s.iloc[-1])  # 终点净值
    except (ValueError, TypeError):
        return 0.0
    if not np.isfinite(start) or not np.isfinite(end) or math.isclose(start, 0.0, abs_tol=1e-12):
        return 0.0  # 起点为零/非有限无法定义 CAGR
    # n 已在上文按原序列有效跨度计算（含 NaN 缺口），此处不再用 len(s)-1 覆盖
    if n <= 0:
        return 0.0
    # CAGR 年化 — guard non-positive ratio before pow: float**float on a
    # negative base yields complex (no exception), escaping isnan/isinf guards
    ratio = end / start
    if not np.isfinite(ratio) or ratio <= 0:
        return 0.0
    try:
        ann = ratio ** (periods / n) - 1
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as e:
        import logging

        logging.getLogger(__name__).warning("annual_return computation failed: %s", e)
        return 0.0
    if not np.isfinite(ann):
        return 0.0
    return float(np.real(ann))


def turnover(
    positions: pd.DataFrame | pd.Series | None = None,
    weights: np.ndarray | list[float] | pd.Series | None = None,
) -> float:
    """换手率估计：有持仓时取日均绝对变动，否则为 0。

    多资产场景下，对每行各标的绝对变动求和后取均值，即真实换手。

    weights fallback `/2` — half-turnover semantics:
        当无 positions 时，用权重差分近似换手。sum(|Δw|) 统计了买卖双边的
        总变动（买入 amount = 卖出 amount），而换手率按惯例为单边口径，
        故除以 2 得到单边换手率，避免对同一资金流动双计。例如 w=[0,1,0]
        时 sum(|Δw|)=2，但单边换手应为 1.0。
    口径统一：与 positions 路径一致取日均（/(n-1)），w=[0,1,0,1,0] 得 0.5。
    """
    import logging

    logger = logging.getLogger(__name__)
    if positions is not None:
        try:
            if isinstance(positions, pd.DataFrame):
                # 多资产：每日各标的绝对变动求和后取均值 — single-side (/2)
                # semantics consistent with the weights fallback below
                _pdf = positions.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna(how="all")
                diff = _pdf.diff().abs().sum(axis=1).dropna()
                if not diff.empty:
                    _m = float(diff.sum() / 2 / len(diff))
                    return _m if np.isfinite(_m) else 0.0
            elif isinstance(positions, pd.Series):
                _ps = pd.to_numeric(positions, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
                diff = _ps.diff().abs().dropna()
                if not diff.empty:
                    _m = float(diff.sum() / 2 / len(diff))
                    return _m if np.isfinite(_m) else 0.0
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("turnover computation failed: %s", e)
            return 0.0
    # 无持仓时的权重回落：稳定权重视为低换手（日均单边口径，与 positions 路径一致）；
    # 输入含 NaN/Inf 视为不可信，直接回落 0.0，不清洗后计算（防脏权重伪装有效换手）
    if weights is not None:
        try:
            _arr = np.asarray(weights, dtype=float)
            if _arr.size > 1 and not np.all(np.isfinite(_arr)):
                return 0.0
            _w = pd.to_numeric(pd.Series(weights), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if len(_w) > 1:
                _d = _w.diff().abs().dropna()
                if not _d.empty:
                    _m = float(_d.sum() / 2 / len(_d))
                    return _m if np.isfinite(_m) else 0.0
            return 0.0
        except (ValueError, TypeError) as e:
            logger.warning("turnover weights fallback failed: %s", e)
            return 0.0
    return 0.0


def compute_metrics(
    equity_series: pd.Series | pd.DataFrame,
    costs: float = 0.0,
    positions: pd.DataFrame | pd.Series | None = None,
    weights: np.ndarray | list[float] | pd.Series | None = None,
) -> dict:
    """汇总常规回测指标：sharpe/annual_return/max_drawdown/turnover/volatility/cumulative_return。

    costs: 若非零则按 costs 对权益做净收益调整（net returns = gross - costs），
           避免 unused 参数误导；若 equity 已是净权益则 costs 接近 0，影响可忽略。
           语义为 additive per-bar drag（每 Bar 固定扣除），与 engine 中
           turnover-scaled 成本（costs * turnover_rate）区分：本函数为轻量
           指标层面的 additive 估计，不做 multiplicative (1-costs) 复利缩放，
           也不按换手率缩放；如需换手敏感成本请在 BacktestEngine 层计算。
    """
    import logging
    import math

    logger = logging.getLogger(__name__)
    # 归一化为 Series
    if isinstance(equity_series, pd.DataFrame):
        # 优先 equity 列，否则取首列
        if "equity" in equity_series.columns:
            s = equity_series["equity"]
        else:
            s = equity_series.iloc[:, 0]
    else:
        s = equity_series

    s = pd.Series(s) if not isinstance(s, pd.Series) else s

    # 数值化并剔除缺失，空序列直接回落零指标
    try:
        s = pd.to_numeric(s, errors="coerce").dropna()
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("equity to_numeric failed: %s", e, exc_info=True)
        s = pd.Series(dtype=float)
    if s.empty:
        return {
            "sharpe": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0,
            "volatility": 0.0,
            "cumulative_return": 0.0,
        }

    # Wire costs (additive per-bar drag, not multiplicative): net returns = gross - costs
    # Additive drag subtracts a fixed cost each bar; multiplicative would be (1-costs) scaling.
    # Contrast with BacktestEngine turnover-scaled costs (costs * turnover_rate) — this
    # lightweight metrics path is intentionally not turnover-scaled.
    try:
        costs_f = float(costs) if costs is not None else 0.0
    except (ValueError, TypeError):
        costs_f = 0.0
    if costs_f and np.isfinite(costs_f) and not math.isclose(costs_f, 0.0, abs_tol=1e-12) and len(s) >= 2:
        try:
            gross_ret = s.pct_change().fillna(0.0).replace([np.inf, -np.inf], 0.0)
            net_ret = gross_ret - costs_f
            # 首期不扣费：首 Bar 的 gross_ret 为 0，不应扣除 costs，避免 double-deduct 建仓费
            if len(net_ret) > 0:
                net_ret.iloc[0] = float(gross_ret.iloc[0])
            # 使用 cumprod 重建净权益曲线 — bankrupt bar (1+net_ret <= 0) 不丢弃
            # 整条成本调整：按有限责任逐 Bar 截断（破产吸收，后续收益按幸存份额
            # 复利），保留成本伤害信号，同时杜绝负权益流入 annual_return
            # （complex 崩溃）与 max_drawdown
            net_equity = (1 + net_ret).clip(lower=0.0).cumprod() * float(s.iloc[0])
            net_equity.index = s.index
            net_equity = pd.to_numeric(net_equity, errors="coerce").dropna()
            if ((1 + net_ret) <= 0).any():
                logger.warning("compute_metrics costs %s bankrupts a bar; flooring per-bar at 0", costs_f)
            if not net_equity.empty and bool((net_equity >= 0).all()) and np.isfinite(net_equity.iloc[-1]):
                s = net_equity
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("compute_metrics costs wiring failed: %s", e, exc_info=True)

    sr = sharpe_ratio(s)
    ar = annual_return(s)
    mdd = max_drawdown(s)
    to = turnover(positions, weights)

    # 年化波动率与累计收益（容差零判断 + 有限性清洗，不泄漏 NaN/Inf）
    try:
        ret = s.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        if not ret.empty:
            _std = float(ret.std(ddof=1))
            if np.isfinite(_std) and not math.isclose(_std, 0.0, abs_tol=1e-9):
                vol = float(_std * np.sqrt(252)) if np.isfinite(_std * np.sqrt(252)) else 0.0  # 252 交易日年化
            else:
                vol = 0.0
        else:
            vol = 0.0
        _s0 = float(s.iloc[0])
        _s1 = float(s.iloc[-1])
        if np.isfinite(_s0) and np.isfinite(_s1) and not math.isclose(_s0, 0.0, abs_tol=1e-12):
            cum_ret = float(_s1 / _s0 - 1)
            if not np.isfinite(cum_ret):
                cum_ret = 0.0
        else:
            cum_ret = 0.0
    except (ValueError, TypeError, AttributeError, ZeroDivisionError) as e:
        logger.warning("compute_metrics ret/vol failed: %s", e)
        vol = 0.0
        cum_ret = 0.0

    return {
        "sharpe": sr,
        "annual_return": ar,
        "max_drawdown": mdd,
        "turnover": to,
        "volatility": vol,
        "cumulative_return": cum_ret,
    }
