"""Minimal quantlib indicators — pure pandas/numpy, no external deps."""
import pandas as pd
import numpy as np


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average: rolling mean."""
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average."""
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Relative Strength Index 0-100.

    Classic formula: 100 - (100 / (1 + RS)), RS = avg_gain / avg_loss
    Uses Wilder's smoothing via ewm(alpha=1/n). Handles n > len(s) by
    using min_periods=1 and filling edge cases to stay in [0,100].
    """
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder's smoothing: alpha = 1/n
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()

    rs = avg_gain / avg_loss
    rsi_series = 100 - (100 / (1 + rs))

    # Where avg_loss == 0 → RSI 100 (pure gains). Clamp NaN/inf.
    # If avg_loss is 0, rs is inf → rsi 100 already, but for exact 0-division produce inf handling
    rsi_series = rsi_series.where(avg_loss != 0, 100.0)
    # Where both avg_gain and avg_loss are 0 (flat series) → 50
    # The line above sets those to 100; correct flat case to 50 if avg_gain ==0 as well
    flat_mask = (avg_gain == 0) & (avg_loss == 0)
    rsi_series = rsi_series.where(~flat_mask, 50.0)

    # Any remaining NaN (e.g., first element with no history) → 50 to stay in range
    rsi_series = rsi_series.fillna(50.0)
    # Clip to [0,100] for safety
    rsi_series = rsi_series.clip(lower=0, upper=100)
    return rsi_series


def bollinger(s: pd.Series, n: int = 20, k: float = 2):
    """Bollinger Bands: returns (mid, upper, lower)."""
    mid = s.rolling(n).mean()
    std = s.rolling(n).std()
    upper = mid + k * std
    lower = mid - k * std
    return mid, upper, lower


def max_drawdown(equity: pd.Series) -> float:
    """Maximum drawdown as most negative (equity / cummax - 1).min()."""
    cummax = equity.cummax()
    dd = equity / cummax - 1
    return float(dd.min())
