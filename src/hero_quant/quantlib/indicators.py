"""Production-core quantlib indicators — pure pandas/numpy/scipy, no external deps.

Implements: sma, ema, rsi (Wilder/EWM), bollinger, macd, max_drawdown.
Handles Series/DataFrame/list/ndarray, empty, NaN/inf guards, window validation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _to_series(x, name: str = "value") -> pd.Series:
    """Coerce input to pd.Series handling DataFrame/list/ndarray/Series.

    - DataFrame: use 'close' if exists else first column
    - list/ndarray: convert
    - Series: copy with numeric coercion
    Replaces inf with NaN, coerces to numeric.
    """
    if isinstance(x, pd.DataFrame):
        if "close" in x.columns:
            s = x["close"]
        elif "equity" in x.columns:
            s = x["equity"]
        else:
            s = x.iloc[:, 0] if x.shape[1] > 0 else pd.Series(dtype=float)
        s = pd.Series(s)
    elif isinstance(x, pd.Series):
        s = x.copy()
    else:
        # list, ndarray, etc.
        try:
            s = pd.Series(x)
        except Exception:
            s = pd.Series(dtype=float)

    # coerce to numeric, inf -> NaN
    s = pd.to_numeric(s, errors="coerce")
    # replace inf (in case to_numeric didn't)
    s = s.replace([np.inf, -np.inf], np.nan)
    return s


def _validate_window(window, default: int = 20) -> int:
    try:
        w = int(window)
        if w <= 0:
            return default
        return w
    except Exception:
        return default


def sma(series, window: int = 20, *args, **kwargs) -> pd.Series:
    """Simple moving average: rolling mean.

    Args:
        series: Series/DataFrame/list
        window: rolling window (also accepts n, period, span as aliases)
    """
    # alias handling: sma(s, 3) or sma(s, n=3) or window as second positional
    if args:
        # if window was passed as series and second arg is window
        pass
    # kwargs aliases
    if "n" in kwargs:
        window = kwargs["n"]
    if "period" in kwargs:
        window = kwargs["period"]
    if "span" in kwargs:
        window = kwargs["span"]
    # also handle window passed as e.g. window= None
    n = _validate_window(window, default=20)
    s = _to_series(series)
    if s.empty:
        return s
    # keep original index
    return s.rolling(window=n, min_periods=n).mean()


def ema(series, span: int = 20, *args, **kwargs) -> pd.Series:
    """Exponential moving average via ewm(span=span, adjust=False)."""
    # alias handling
    if "n" in kwargs:
        span = kwargs["n"]
    if "window" in kwargs:
        span = kwargs["window"]
    if "period" in kwargs:
        span = kwargs["period"]
    # if span passed positionally as second arg via *args? not needed
    n = _validate_window(span, default=20)
    s = _to_series(series)
    if s.empty:
        return s
    return s.ewm(span=n, adjust=False, min_periods=1).mean()


def rsi(series, period: int = 14, *args, **kwargs) -> pd.Series:
    """Relative Strength Index 0-100 using Wilder's smoothing (ewm alpha=1/period).

    Replicates vibe 2026-08-11 fix: use ewm not simple rolling.
    Handles n > len(s) via min_periods=1, NaN/inf guards, clips to [0,100].
    """
    # alias handling
    if "n" in kwargs:
        period = kwargs["n"]
    if "window" in kwargs:
        period = kwargs["window"]
    if "span" in kwargs:
        period = kwargs["span"]
    # positional alias if period passed as second arg without keyword and span is default?
    # already covered by signature
    n = _validate_window(period, default=14)
    s = _to_series(series)
    if s.empty:
        return pd.Series(dtype=float)
    # if series length 1, diff is NaN -> fillna
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder's smoothing: alpha = 1/n
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()

    rs = avg_gain / avg_loss
    rsi_series = 100 - (100 / (1 + rs))

    # Where avg_loss == 0 → RSI 100 (pure gains)
    rsi_series = rsi_series.where(avg_loss != 0, 100.0)
    # Where both avg_gain and avg_loss are 0 (flat series) → 50
    flat_mask = (avg_gain == 0) & (avg_loss == 0)
    rsi_series = rsi_series.where(~flat_mask, 50.0)

    # Any remaining NaN (e.g., first element) → 50
    rsi_series = rsi_series.fillna(50.0)
    # inf guard
    rsi_series = rsi_series.replace([np.inf, -np.inf], np.nan).fillna(50.0)
    # Clip to [0,100]
    rsi_series = rsi_series.clip(lower=0, upper=100)
    return rsi_series


def bollinger(series, window: int = 20, num_std: float = 2.0, *args, **kwargs):
    """Bollinger Bands: returns (middle, upper, lower) via rolling mean+std.

    Aliases: bollinger(s, n=20, k=2), bollinger(s, window, num_std), bollinger(s, 20, 2)
    """
    # kwargs aliases
    if "n" in kwargs:
        window = kwargs["n"]
    if "k" in kwargs:
        num_std = kwargs["k"]
    if "std" in kwargs:
        num_std = kwargs["std"]
    if "num_std" in kwargs:
        num_std = kwargs["num_std"]
    # handle positional args: if called as bollinger(s, n, k) we already have window,num_std
    # but if called with args excess, handle
    # e.g. bollinger(s, 20, 2) -> ok. If someone does bollinger(s, n=20,k=2) covered.

    n = _validate_window(window, default=20)
    try:
        k = float(num_std)
    except Exception:
        k = 2.0
    if k < 0:
        k = 2.0

    s = _to_series(series)
    if s.empty:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    mid = s.rolling(window=n, min_periods=n).mean()
    # pandas std uses ddof=1 default; use same for consistency
    std = s.rolling(window=n, min_periods=n).std(ddof=1)
    # fill std NaN with 0 for early periods? keep NaN but upper/lower will be NaN as well
    upper = mid + k * std
    lower = mid - k * std
    # inf guards
    mid = mid.replace([np.inf, -np.inf], np.nan)
    upper = upper.replace([np.inf, -np.inf], np.nan)
    lower = lower.replace([np.inf, -np.inf], np.nan)
    return mid, upper, lower


def macd(series, fast: int = 12, slow: int = 26, signal: int = 9, *args, **kwargs):
    """MACD via EMA: returns (macd_line, signal_line, hist).

    macd = ema(fast) - ema(slow)
    signal = ema(macd, signal)
    hist = macd - signal
    """
    # kwargs aliases
    if "n_fast" in kwargs:
        fast = kwargs["n_fast"]
    if "n_slow" in kwargs:
        slow = kwargs["n_slow"]
    if "n_signal" in kwargs:
        signal = kwargs["n_signal"]
    # also allow window/fast/slow/signal interchange
    if "window" in kwargs and fast == 12 and "fast" not in kwargs:
        fast = kwargs["window"]

    fast_n = _validate_window(fast, default=12)
    slow_n = _validate_window(slow, default=26)
    signal_n = _validate_window(signal, default=9)

    s = _to_series(series)
    if s.empty:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    ema_fast = s.ewm(span=fast_n, adjust=False, min_periods=1).mean()
    ema_slow = s.ewm(span=slow_n, adjust=False, min_periods=1).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_n, adjust=False, min_periods=1).mean()
    hist = macd_line - signal_line

    # inf guards
    macd_line = macd_line.replace([np.inf, -np.inf], np.nan)
    signal_line = signal_line.replace([np.inf, -np.inf], np.nan)
    hist = hist.replace([np.inf, -np.inf], np.nan)

    return macd_line, signal_line, hist


def max_drawdown(equity) -> float:
    """Maximum drawdown as most negative (equity / cummax - 1).min().

    Handles Series/DataFrame/list, empty, NaN/inf guards.
    Returns 0.0 for empty or invalid input.
    """
    s = _to_series(equity)
    # dropna for cummax calc but keep logic: if empty return 0
    if s.empty:
        return 0.0
    # drop NaN for computation; if all NaN return 0
    s_clean = s.dropna()
    if s_clean.empty:
        return 0.0
    # replace 0 cummax to avoid division by zero -> treat as NaN then fill
    cummax = s_clean.cummax()
    # avoid division by zero: where cummax ==0, set dd to 0
    # cummax can be 0 if equity starts at 0; handle
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = s_clean / cummax - 1.0
    dd = dd.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    try:
        mdd = float(dd.min())
    except Exception:
        return 0.0
    if np.isnan(mdd) or np.isinf(mdd):
        return 0.0
    # Most negative drawdown; if positive (never drawdown) return 0? Keep as min which would be 0.0
    # Ensure within [-1, 0]
    if mdd > 0:
        mdd = 0.0
    return mdd
