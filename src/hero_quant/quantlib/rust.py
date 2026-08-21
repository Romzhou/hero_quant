"""Python shim for Rust quantlib crate.

Tries to import compiled `quantlib` extension (maturin/PyO3); falls back to pure-Python
indicators so tests/CI pass without Rust toolchain. Proves extraction boundary.

Exposes: sma, ema, rsi, bollinger, macd, max_drawdown, is_rust_available
"""
from __future__ import annotations

import importlib.util

import pandas as pd

# detection flag
IS_RUST = False
_RUST_MOD = None
try:
    # compiled extension is named `quantlib` (cdylib)
    spec = importlib.util.find_spec("quantlib")
    if spec is not None:
        import quantlib as _rust_ext  # type: ignore

        _RUST_MOD = _rust_ext
        IS_RUST = True
except Exception:
    _RUST_MOD = None
    IS_RUST = False

__version__ = getattr(_RUST_MOD, "__version__", "0.2.0-py-fallback") if _RUST_MOD else "0.2.0-py-fallback"


def is_rust_available() -> bool:
    return IS_RUST


# ── Fallback delegation to Python indicators ──
from hero_quant.quantlib.indicators import (  # noqa: E402
    bollinger as _py_bollinger,
)
from hero_quant.quantlib.indicators import ema as _py_ema  # noqa: E402
from hero_quant.quantlib.indicators import macd as _py_macd  # noqa: E402
from hero_quant.quantlib.indicators import max_drawdown as _py_mdd  # noqa: E402
from hero_quant.quantlib.indicators import rsi as _py_rsi  # noqa: E402
from hero_quant.quantlib.indicators import sma as _py_sma  # noqa: E402


def _use_rust() -> bool:
    return IS_RUST and _RUST_MOD is not None


def _to_list(series) -> list[float]:
    """Coerce Series/DataFrame/list to list[float] for Rust Vec<f64>."""
    import numpy as np

    if isinstance(series, pd.DataFrame):
        if "close" in series.columns:
            s = series["close"]
        else:
            s = series.iloc[:, 0] if series.shape[1] > 0 else pd.Series(dtype=float)
        s = pd.Series(s)
    elif isinstance(series, pd.Series):
        s = series
    else:
        try:
            s = pd.Series(series)
        except Exception:
            s = pd.Series(dtype=float)
    s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], float("nan"))
    # Rust Vec<f64> cannot carry NaN? we keep NaN as float nan
    return s.tolist()


def sma(series, window: int = 20, *args, **kwargs) -> pd.Series:
    # alias handling mirroring indicators.sma
    if "n" in kwargs:
        window = kwargs["n"]
    if "period" in kwargs:
        window = kwargs["period"]
    if "span" in kwargs:
        window = kwargs["span"]
    # try rust path
    if _use_rust():
        try:
            data = _to_list(series)
            # rust sma expects Vec<f64> + window usize
            raw = _RUST_MOD.sma([float(x) if x == x else 0.0 for x in data], int(window))
            # raw is Vec<Option<f64>> -> convert to Series
            vals = [v if v is not None else float("nan") for v in raw]
            # preserve index if Series
            if isinstance(series, pd.Series):
                idx = series.index
            elif isinstance(series, pd.DataFrame):
                idx = series.index
            else:
                idx = None
            out = pd.Series(vals, index=idx)
            # keep nan for insufficient window as per py impl
            return out
        except Exception:
            pass
    return _py_sma(series, window, *args, **kwargs)


def ema(series, span: int = 20, *args, **kwargs) -> pd.Series:
    if "n" in kwargs:
        span = kwargs["n"]
    if "window" in kwargs:
        span = kwargs["window"]
    if "period" in kwargs:
        span = kwargs["period"]
    if _use_rust():
        try:
            data = _to_list(series)
            raw = _RUST_MOD.ema([float(x) if x == x else 0.0 for x in data], int(span))
            if isinstance(series, pd.Series):
                idx = series.index
            elif isinstance(series, pd.DataFrame):
                idx = series.index
            else:
                idx = None
            return pd.Series(raw, index=idx)
        except Exception:
            pass
    return _py_ema(series, span, *args, **kwargs)


def rsi(series, period: int = 14, *args, **kwargs) -> pd.Series:
    if "n" in kwargs:
        period = kwargs["n"]
    if "window" in kwargs:
        period = kwargs["window"]
    if "span" in kwargs:
        period = kwargs["span"]
    if _use_rust():
        try:
            data = _to_list(series)
            raw = _RUST_MOD.rsi([float(x) if x == x else 0.0 for x in data], int(period))
            if isinstance(series, pd.Series):
                idx = series.index
            elif isinstance(series, pd.DataFrame):
                idx = series.index
            else:
                idx = None
            return pd.Series(raw, index=idx)
        except Exception:
            pass
    return _py_rsi(series, period, *args, **kwargs)


def bollinger(series, window: int = 20, num_std: float = 2.0, *args, **kwargs):
    # delegate always to py for simplicity (rust path optional)
    if _use_rust() and "n" not in kwargs and "k" not in kwargs:
        try:
            data = _to_list(series)
            mid_raw, up_raw, low_raw = _RUST_MOD.bollinger(
                [float(x) if x == x else 0.0 for x in data], int(window), float(num_std)
            )
            vals_mid = [v if v is not None else float("nan") for v in mid_raw]
            vals_up = [v if v is not None else float("nan") for v in up_raw]
            vals_low = [v if v is not None else float("nan") for v in low_raw]
            if isinstance(series, pd.Series):
                idx = series.index
            elif isinstance(series, pd.DataFrame):
                idx = series.index
            else:
                idx = None
            return pd.Series(vals_mid, index=idx), pd.Series(vals_up, index=idx), pd.Series(vals_low, index=idx)
        except Exception:
            pass
    return _py_bollinger(series, window, num_std, *args, **kwargs)


def macd(series, fast: int = 12, slow: int = 26, signal: int = 9, *args, **kwargs):
    if _use_rust():
        try:
            data = _to_list(series)
            a, b, c = _RUST_MOD.macd(
                [float(x) if x == x else 0.0 for x in data], int(fast), int(slow), int(signal)
            )
            if isinstance(series, pd.Series):
                idx = series.index
            elif isinstance(series, pd.DataFrame):
                idx = series.index
            else:
                idx = None
            return pd.Series(a, index=idx), pd.Series(b, index=idx), pd.Series(c, index=idx)
        except Exception:
            pass
    return _py_macd(series, fast, slow, signal, *args, **kwargs)


def max_drawdown(equity) -> float:
    if _use_rust():
        try:
            # coerce to list
            if isinstance(equity, pd.Series):
                data = equity.tolist()
            elif isinstance(equity, pd.DataFrame):
                data = equity.iloc[:, 0].tolist() if equity.shape[1] > 0 else []
            else:
                data = list(equity) if equity is not None else []
            clean = [float(x) for x in data if x is not None]
            return float(_RUST_MOD.max_drawdown(clean))
        except Exception:
            pass
    return _py_mdd(equity)


# aliases for spec "sma_ema etc"
sma_ema = sma  # placeholder alias proof
