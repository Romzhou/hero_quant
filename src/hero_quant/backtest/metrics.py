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
    if equity is None or len(equity) < 2:
        return 0.0
    # 日收益序列
    ret = equity.pct_change().dropna()
    if ret.empty or ret.std(ddof=1) == 0 or np.isnan(ret.std(ddof=1)):
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
    # 统一为 Series
    s = pd.Series(equity) if not isinstance(equity, pd.Series) else equity
    # 兼容单列 DataFrame 传入
    if isinstance(equity, pd.DataFrame):
        s = equity.iloc[:, 0]
    s = pd.to_numeric(s, errors="coerce")
    cummax = s.cummax()  # 滚动峰值
    # 避免除零：cummax 为 0 处回撤归零（inf 替换为 NaN 后填 0）
    cummax = cummax.replace(0, np.nan)
    dd = s / cummax - 1.0
    dd = dd.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # 最深回撤（最小值即最负）
    mdd = float(dd.min()) if not dd.empty else 0.0
    if np.isnan(mdd) or np.isinf(mdd):
        return 0.0
    return mdd


def annual_return(equity: pd.Series, periods: int = 252) -> float:
    """年化收益（CAGR）：(end/start)^(252/n)-1，起点为 0 或空回落 0。"""
    if equity is None or len(equity) < 2:
        return 0.0
    s = pd.Series(equity) if not isinstance(equity, pd.Series) else equity
    if isinstance(equity, pd.DataFrame):
        s = equity.iloc[:, 0]
    start = float(s.iloc[0])  # 起点净值
    end = float(s.iloc[-1])  # 终点净值
    if start == 0 or np.isnan(start) or np.isnan(end):
        return 0.0  # 起点为零无法定义 CAGR
    # guard len<2 already returned; n = number of periods = len-1 (off-by-one fix)
    n = len(s) - 1
    if n <= 0:
        return 0.0
    # CAGR 年化
    try:
        ann = (end / start) ** (periods / n) - 1
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as e:
        import logging

        logging.getLogger(__name__).warning("annual_return computation failed: %s", e)
        ann = 0.0
    if np.isnan(ann) or np.isinf(ann):
        return 0.0
    return float(ann)


def turnover(positions: pd.DataFrame | pd.Series | None = None, weights=None) -> float:
    """换手率估计：有持仓时取日均绝对变动，否则为 0。

    多资产场景下，对每行各标的绝对变动求和后取均值，即真实换手。
    """
    import logging

    logger = logging.getLogger(__name__)
    if positions is not None:
        try:
            if isinstance(positions, pd.DataFrame):
                # 多资产：每日各标的绝对变动求和后取均值
                diff = positions.diff().abs().sum(axis=1).dropna()
                if not diff.empty:
                    return float(diff.mean())
            elif isinstance(positions, pd.Series):
                diff = positions.diff().abs().dropna()
                if not diff.empty:
                    return float(diff.mean())
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("turnover computation failed: %s", e)
            return 0.0
    # 无持仓时的权重回落：稳定权重视为低换手
    if weights is not None:
        try:
            _w = np.asarray(weights, dtype=float)
            # 日频再平衡代理，暂视为 0 换手
            return 0.0
        except (ValueError, TypeError) as e:
            logger.warning("turnover weights fallback failed: %s", e)
            return 0.0
    return 0.0


def compute_metrics(equity_series: pd.Series | pd.DataFrame, costs: float = 0.0, positions=None, weights=None) -> dict:
    """汇总常规回测指标：sharpe/annual_return/max_drawdown/turnover/volatility/cumulative_return。

    costs: 若非零则按 costs 对权益做净收益调整（net returns = gross - costs），
           避免 unused 参数误导；若 equity 已是净权益则 costs 接近 0，影响可忽略。
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

    # Wire costs: net returns = gross - costs (per-bar drag)
    try:
        costs_f = float(costs) if costs is not None else 0.0
    except (ValueError, TypeError):
        costs_f = 0.0
    if costs_f and np.isfinite(costs_f) and not math.isclose(costs_f, 0.0, abs_tol=1e-12) and len(s) >= 2:
        try:
            gross_ret = s.pct_change().fillna(0.0).replace([np.inf, -np.inf], 0.0)
            net_ret = gross_ret - costs_f
            # 首期不扣费（建仓已计入 turnover 的场景下首期已处理，此处首期保留 gross）
            # 为保持幂等：若 s 已是净权益，再扣一次会低估；仅当 costs 显著且调用方显式传入时执行
            # 使用 cumprod 重建净权益曲线
            net_equity = (1 + net_ret).cumprod() * float(s.iloc[0])
            net_equity.index = s.index
            net_equity = pd.to_numeric(net_equity, errors="coerce").dropna()
            if not net_equity.empty and np.isfinite(net_equity.iloc[-1]):
                s = net_equity
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("compute_metrics costs wiring failed: %s", e, exc_info=True)

    sr = sharpe_ratio(s)
    ar = annual_return(s)
    mdd = max_drawdown(s)
    to = turnover(positions, weights)

    # 年化波动率与累计收益
    try:
        ret = s.pct_change().dropna()
        vol = float(ret.std(ddof=1) * np.sqrt(252)) if not ret.empty and ret.std(ddof=1) != 0 else 0.0  # 252 交易日年化
        cum_ret = float(s.iloc[-1] / s.iloc[0] - 1) if s.iloc[0] != 0 else 0.0
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
