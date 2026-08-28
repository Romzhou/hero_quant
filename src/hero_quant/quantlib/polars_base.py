"""Polars 向量化基座：与 pandas 指标保持对齐的向量化实现。

职责：提供 polars 加速的 sma（及后续多算子脚手架），返回 pandas Series 以便直接替换。
架构位置：quantlib 向量化层，未来与 Rust 内核协同；当前保证与 indicators.sma 的滚动语义一致。
关键设计：rolling_mean 的 min_samples/min_periods 与 pandas 对齐；保留原始索引与名称。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import polars as pl
except ImportError as _e:  # lazy optional dep: pip install hero-quant[vector]
    pl = None  # type: ignore[assignment]
    _polars_import_error = _e
else:
    _polars_import_error = None


def _require_polars() -> None:
    """确保 polars 已安装，否则给出明确安装指引。"""
    if pl is None:
        raise ImportError(
            "polars is required for vectorized indicators. Install with: pip install hero-quant[vector]"
        ) from _polars_import_error


def _validate_window(window, default: int = 20) -> int:
    """校验窗口参数：非正/非法抛 ValueError 由调用方处理（fail-visible）。"""
    if window is None:
        return default
    try:
        w = int(window)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid window {window!r}: {e}") from e
    if w <= 0:
        raise ValueError(f"window must be >0, got {w}")
    return w


def _to_series(x, name: str = "value") -> pd.Series:
    """与 indicators._to_series 同步的归一化逻辑，保证两者对齐。fail-visible on construction."""
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
        except (ValueError, TypeError) as e:
            raise ValueError(f"cannot convert to Series: {e}") from e
    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    return s


def sma_polars(series, window: int = 20, *args, **kwargs) -> pd.Series:
    """SMA（Polars 加速）：rolling_mean(window, min_samples=n) 与 pandas 语义一致。"""
    if args:
        raise TypeError(f"sma_polars: unexpected positional args {args}")
    alias_keys = [k for k in ("n", "period", "span") if k in kwargs]
    if len(alias_keys) > 1:
        raise TypeError(f"sma: conflicting aliases {alias_keys}, specify only one")
    if alias_keys:
        window = kwargs[alias_keys[0]]

    n = _validate_window(window, default=20)
    s = _to_series(series)

    if s.empty:
        return s

    _require_polars()

    idx = s.index
    orig_name = s.name

    # 零拷贝构造：优先用 numpy 直通，避免 2-3 次复制
    try:
        arr = s.to_numpy(dtype=float, na_value=np.nan)
        pl_s = pl.Series("value", arr, dtype=pl.Float64, strict=False)
    except (ValueError, TypeError) as e:
        raise ValueError(f"polars Series construction failed: {e}") from e

    # min_samples/min_periods 与 pandas min_periods 对齐，保证不足窗口为 null/NaN
    # 优先检测签名而非捕获所有异常
    res_list = None
    try:
        res_list = pl_s.rolling_mean(window_size=n, min_samples=n).to_list()
    except TypeError:
        # 兼容旧版 polars API
        try:
            res_list = pl_s.rolling_mean(window_size=n, min_periods=n).to_list()
        except (ValueError, TypeError) as e:
            raise ValueError(f"rolling_mean failed window={n}: {e}") from e

    # 转回 pandas 并保留索引；None 经 dtype float 自动转为 NaN
    result = pd.Series(res_list, index=idx, dtype=float)
    if orig_name is not None:
        result.name = orig_name

    result = result.replace([np.inf, -np.inf], np.nan)
    return result


# 向量化算子脚手架：后续将由 polars/Rust 内核算子替换，目前委托 pandas 保持对齐
def ema_polars(series, span: int = 20, **kwargs) -> pd.Series:
    """EMA（Polars 占位）：暂委托 pandas ewm，保持对齐直至 Rust 内核就绪。"""
    from hero_quant.quantlib.indicators import ema

    return ema(series, span=span, **kwargs)


def rsi_polars(series, period: int = 14, **kwargs) -> pd.Series:
    """RSI（Polars 占位）：暂委托 pandas Wilder 实现保持对齐。"""
    from hero_quant.quantlib.indicators import rsi

    return rsi(series, period=period, **kwargs)
