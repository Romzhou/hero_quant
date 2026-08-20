"""Polars vector base — vectorized indicators with pandas parity.

Design aligned to Rust+Polars future (Task 6):
- Provides polars-backed SMA (and scaffolding for 60 operators)
- Guarantees parity with pandas indicators.sma via rolling_mean
- API returns pandas Series preserving index for drop-in use
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl


def _validate_window(window, default: int = 20) -> int:
    try:
        w = int(window)
        if w <= 0:
            return default
        return w
    except Exception:
        return default


def _to_series(x, name: str = "value") -> pd.Series:
    """Mirror indicators._to_series for parity."""
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
        try:
            s = pd.Series(x)
        except Exception:
            s = pd.Series(dtype=float)
    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    return s


def sma_polars(series, window: int = 20, *args, **kwargs) -> pd.Series:
    """Polars-backed SMA via rolling_mean, pandas parity.

    Mirrors hero_quant.quantlib.indicators.sma semantics:
    - rolling(window=n, min_periods=n).mean()
    - handles Series/DataFrame/list/ndarray, empty, NaN/inf, aliases

    Args:
        series: input data
        window: rolling window (aliases: n, period, span)
    """
    if "n" in kwargs:
        window = kwargs["n"]
    if "period" in kwargs:
        window = kwargs["period"]
    if "span" in kwargs:
        window = kwargs["span"]

    n = _validate_window(window, default=20)
    s = _to_series(series)

    if s.empty:
        return s

    idx = s.index
    orig_name = s.name

    # Build polars Series as Float64 to handle NaN correctly
    # Use numpy float conversion to avoid Int64 NaN construction error
    try:
        values = s.values.astype(float).tolist()
    except Exception:
        try:
            values = [float(v) if v is not None else None for v in s.tolist()]
        except Exception:
            values = s.tolist()

    pl_s = pl.Series("value", values, dtype=pl.Float64, strict=False)

    # Polars rolling_mean: use min_samples=n to match pandas min_periods=n
    try:
        res_list = pl_s.rolling_mean(window_size=n, min_samples=n).to_list()
    except TypeError:
        # fallback for older polars API using min_periods
        res_list = pl_s.rolling_mean(window_size=n, min_periods=n).to_list()

    # Convert back to pandas preserving index; None -> NaN via dtype float
    result = pd.Series(res_list, index=idx, dtype=float)
    if orig_name is not None:
        result.name = orig_name

    result = result.replace([np.inf, -np.inf], np.nan)
    return result


# Scaffolding for future 60 operators (Rust stubs) — vectorized wrappers
# These expose pandas API but will be backed by polars/rust kernels
def ema_polars(series, span: int = 20, **kwargs) -> pd.Series:
    """Placeholder polars EMA — delegates to pandas ewm for parity until Rust kernel."""
    from hero_quant.quantlib.indicators import ema

    return ema(series, span=span, **kwargs)


def rsi_polars(series, period: int = 14, **kwargs) -> pd.Series:
    """Placeholder polars RSI — delegates to pandas for parity."""
    from hero_quant.quantlib.indicators import rsi

    return rsi(series, period=period, **kwargs)
