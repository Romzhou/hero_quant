"""Metrics for backtest engine - pure pandas/numpy/scipy."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sharpe_ratio(equity: pd.Series, risk_free: float = 0.0, periods: int = 252) -> float:
    """Annualized Sharpe ratio from equity curve.

    Args:
        equity: equity series (index date, value price)
        risk_free: annual risk free rate
        periods: trading periods per year
    """
    if equity is None or len(equity) < 2:
        return 0.0
    # daily returns
    ret = equity.pct_change().dropna()
    if ret.empty or ret.std(ddof=1) == 0 or np.isnan(ret.std(ddof=1)):
        return 0.0
    # excess return
    # risk_free daily
    rf_daily = risk_free / periods
    excess = ret - rf_daily
    sr = excess.mean() / excess.std(ddof=1) * np.sqrt(periods)
    if np.isnan(sr) or np.isinf(sr):
        return 0.0
    return float(sr)


def max_drawdown(equity: pd.Series) -> float:
    """Maximum drawdown (negative value, e.g. -0.05 for 5% drawdown).

    Computes min(equity / cummax - 1).
    """
    if equity is None or len(equity) == 0:
        return 0.0
    # ensure Series
    s = pd.Series(equity) if not isinstance(equity, pd.Series) else equity
    # handle DataFrame with single column
    if isinstance(equity, pd.DataFrame):
        s = equity.iloc[:, 0]
    cummax = s.cummax()
    # avoid division by zero
    dd = s / cummax - 1.0
    # min drawdown (most negative)
    mdd = float(dd.min())
    if np.isnan(mdd):
        return 0.0
    return mdd


def annual_return(equity: pd.Series, periods: int = 252) -> float:
    """Annualized return from equity curve.

    Uses compound annual growth rate.
    """
    if equity is None or len(equity) < 2:
        return 0.0
    s = pd.Series(equity) if not isinstance(equity, pd.Series) else equity
    if isinstance(equity, pd.DataFrame):
        s = equity.iloc[:, 0]
    start = float(s.iloc[0])
    end = float(s.iloc[-1])
    if start == 0 or np.isnan(start) or np.isnan(end):
        return 0.0
    n = len(s)
    # CAGR
    try:
        ann = (end / start) ** (periods / n) - 1
    except Exception:
        ann = 0.0
    if np.isnan(ann) or np.isinf(ann):
        return 0.0
    return float(ann)


def turnover(positions: pd.DataFrame | pd.Series | None = None, weights=None) -> float:
    """Estimate turnover.

    Simplified: if positions provided, turnover = mean absolute change.
    Otherwise 0.0.

    Args:
        positions: DataFrame of positions over time or Series
        weights: optional weights to estimate
    """
    if positions is not None:
        try:
            if isinstance(positions, pd.DataFrame):
                # sum absolute change across assets per day, mean
                diff = positions.diff().abs().sum(axis=1).dropna()
                if not diff.empty:
                    return float(diff.mean())
            elif isinstance(positions, pd.Series):
                diff = positions.diff().abs().dropna()
                if not diff.empty:
                    return float(diff.mean())
        except Exception:
            return 0.0
    # fallback heuristic from weights: stable weights => low turnover
    if weights is not None:
        try:
            w = np.asarray(weights, dtype=float)
            # turnover proxy: if rebalanced daily, turnover ~ 0
            # keep minimal non-zero to indicate costs impact
            return 0.0
        except Exception:
            return 0.0
    return 0.0


def compute_metrics(equity_series: pd.Series | pd.DataFrame, costs: float = 0.0, positions=None, weights=None) -> dict:
    """Compute standard backtest metrics.

    Args:
        equity_series: equity curve (Series or DataFrame with single column)
        costs: transaction costs (for info, not used in calc except turnover)
        positions: optional positions for turnover
        weights: optional weights for turnover

    Returns:
        dict with keys: sharpe, annual_return, max_drawdown, turnover, volatility, cumulative_return
    """
    # Normalize to Series
    if isinstance(equity_series, pd.DataFrame):
        # if DataFrame with 'equity' column or single column
        if "equity" in equity_series.columns:
            s = equity_series["equity"]
        else:
            s = equity_series.iloc[:, 0]
    else:
        s = equity_series

    s = pd.Series(s) if not isinstance(s, pd.Series) else s

    # Ensure numeric
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {
            "sharpe": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0,
            "volatility": 0.0,
            "cumulative_return": 0.0,
        }

    sr = sharpe_ratio(s)
    ar = annual_return(s)
    mdd = max_drawdown(s)
    to = turnover(positions, weights)

    # additional metrics
    ret = s.pct_change().dropna()
    vol = float(ret.std(ddof=1) * np.sqrt(252)) if not ret.empty and ret.std(ddof=1) != 0 else 0.0
    cum_ret = float(s.iloc[-1] / s.iloc[0] - 1) if s.iloc[0] != 0 else 0.0

    return {
        "sharpe": sr,
        "annual_return": ar,
        "max_drawdown": mdd,
        "turnover": to,
        "volatility": vol,
        "cumulative_return": cum_ret,
    }
