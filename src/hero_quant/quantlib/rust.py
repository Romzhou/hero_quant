"""Rust 桥接层：优先调用编译期 quantlib 扩展，缺失时回落至 Python 实现。

职责：为上层提供统一的 sma/ema/rsi/bollinger/macd/max_drawdown 接口，屏蔽是否有 Rust 工具链的差异。
架构位置：quantlib 的薄封装，位于 Python 与 Rust crate 之间，保持与 indicators 的 API 一致。
关键设计：运行时探测 IS_RUST；单点回落保证 CI/本地均可运行；返回 pandas 类型以保持可替换性。
"""
from __future__ import annotations

import importlib.util

import pandas as pd

# 运行时探测：是否可用 Rust 编译扩展
IS_RUST = False
_RUST_MOD = None
try:
    # 编译产物名为 quantlib（cdylib）
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
    """是否已加载 Rust 扩展。"""
    return IS_RUST


# ── 回落至 Python 实现 ──
from hero_quant.quantlib.indicators import (  # noqa: E402
    bollinger as _py_bollinger,
)
from hero_quant.quantlib.indicators import ema as _py_ema  # noqa: E402
from hero_quant.quantlib.indicators import macd as _py_macd  # noqa: E402
from hero_quant.quantlib.indicators import max_drawdown as _py_mdd  # noqa: E402
from hero_quant.quantlib.indicators import rsi as _py_rsi  # noqa: E402
from hero_quant.quantlib.indicators import sma as _py_sma  # noqa: E402


def _use_rust() -> bool:
    """判断是否走 Rust 路径。"""
    return IS_RUST and _RUST_MOD is not None


def _to_list(series) -> list[float]:
    """将 Series/DataFrame/list 归一为 list[float]，以适配 Rust Vec<f64>。"""
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
    # Rust 侧以 NaN 表示缺失，此处保留 NaN
    return s.tolist()


def _prepare_data(series) -> list[float] | None:
    """Centralized NaN detection: if any NaN present, return None to signal fallback.

    Uses pd.isna for robust NaN/None detection (covers float('nan'), np.nan, None).
    Only clean floats are returned for Rust Vec<f64>; corrupt 0.0 coercion is removed.
    """
    data = _to_list(series)
    # pd.isna handles NaN/None/NaT robustly; math.isnan would fail on non-float
    if any(pd.isna(x) for x in data):
        return None
    return [float(x) for x in data]


def sma(series, window: int = 20, *args, **kwargs) -> pd.Series:
    """SMA：优先 Rust，失败回落 Python；语义与 indicators.sma 一致。"""
    # 兼容别名
    if "n" in kwargs:
        window = kwargs["n"]
    if "period" in kwargs:
        window = kwargs["period"]
    if "span" in kwargs:
        window = kwargs["span"]
    # 尝试 Rust 路径 — NaN 输入直接走 Python 回落，避免 0.0 污染
    if _use_rust():
        _prep = _prepare_data(series)
        if _prep is None:
            return _py_sma(series, window, *args, **kwargs)
        try:
            # Rust 期望 Vec<f64> 与窗口大小（已确保无 NaN）
            raw = _RUST_MOD.sma(_prep, int(window))
            # Vec<Option<f64>> 转 Series，None 映射为 NaN
            vals = [v if v is not None else float("nan") for v in raw]
            # 保留原始索引以保持可替换性
            if isinstance(series, pd.Series):
                idx = series.index
            elif isinstance(series, pd.DataFrame):
                idx = series.index
            else:
                idx = None
            out = pd.Series(vals, index=idx)
            # 不足窗口的 NaN 与 Python 实现对齐
            return out
        except Exception:
            pass  # 任意异常均回落 Python
    return _py_sma(series, window, *args, **kwargs)


def ema(series, span: int = 20, *args, **kwargs) -> pd.Series:
    """EMA：优先 Rust，失败回落 Python。"""
    if "n" in kwargs:
        span = kwargs["n"]
    if "window" in kwargs:
        span = kwargs["window"]
    if "period" in kwargs:
        span = kwargs["period"]
    if _use_rust():
        _prep = _prepare_data(series)
        if _prep is None:
            return _py_ema(series, span, *args, **kwargs)
        try:
            raw = _RUST_MOD.ema(_prep, int(span))
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
    """RSI：优先 Rust，失败回落 Python；Wilder EWM 实现。"""
    if "n" in kwargs:
        period = kwargs["n"]
    if "window" in kwargs:
        period = kwargs["window"]
    if "span" in kwargs:
        period = kwargs["span"]
    if _use_rust():
        _prep = _prepare_data(series)
        if _prep is None:
            return _py_rsi(series, period, *args, **kwargs)
        try:
            raw = _RUST_MOD.rsi(_prep, int(period))
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
    """布林带：优先 Rust（无别名时），失败回落 Python。"""
    if _use_rust() and "n" not in kwargs and "k" not in kwargs:
        _prep = _prepare_data(series)
        if _prep is None:
            return _py_bollinger(series, window, num_std, *args, **kwargs)
        try:
            mid_raw, up_raw, low_raw = _RUST_MOD.bollinger(
                _prep, int(window), float(num_std)
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
    """MACD：优先 Rust，失败回落 Python。"""
    if _use_rust():
        _prep = _prepare_data(series)
        if _prep is None:
            return _py_macd(series, fast, slow, signal, *args, **kwargs)
        try:
            a, b, c = _RUST_MOD.macd(
                _prep, int(fast), int(slow), int(signal)
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
    """最大回撤：优先 Rust，失败回落 Python。"""
    if _use_rust():
        try:
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


# 别名占位：保持兼容
sma_ema = sma
