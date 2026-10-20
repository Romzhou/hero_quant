"""指标库：纯 pandas/numpy 的核心技术指标（无外部依赖）。

职责：提供 sma/ema/rsi/bollinger/macd/atr/max_drawdown，供回测/研究/工具调用。
架构位置：quantlib 基础层，被 rust/polars 上层封装复用；保证空数据/NaN/Inf 鲁棒性。
关键设计：rsi 采用 Wilder EWM（alpha=1/n）；bollinger 以滚动均值±k·std；macd 为快慢 EMA 差；
atr 为 Wilder TR 平滑（首值取前 n 个 TR 均值、之后按 (prev*(n-1)+tr)/n 递推）。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _to_series(x, name: str = "value") -> pd.Series:
    """将任意输入归一为数值型 Series：DataFrame 优先 close/equity/首列，非法/Inf 置为 NaN。"""
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
        except (ValueError, TypeError) as e:
            logger.warning("_to_series construction failed for %r: %s", type(x).__name__, e, exc_info=True)
            raise TypeError(f"_to_series: cannot convert {type(x).__name__} to Series: {e}") from e
        except Exception as e:
            logger.warning("_to_series unexpected failure for %r: %s", type(x).__name__, e, exc_info=True)
            raise

    # coerce to numeric, inf -> NaN
    s = pd.to_numeric(s, errors="coerce")
    # replace inf (in case to_numeric didn't)
    s = s.replace([np.inf, -np.inf], np.nan)
    return s


def _validate_window(window, default: int = 20) -> int:
    """校验窗口参数：非正/非法抛异常并带 exc_info，避免静默回落掩盖错误配置。"""
    try:
        w = int(window)
    except (ValueError, TypeError) as e:
        logger.warning("_validate_window invalid window %r: %s", window, e, exc_info=True)
        raise ValueError(f"invalid window {window!r}: must be positive int") from e
    except Exception as e:
        logger.warning("_validate_window unexpected error for %r: %s", window, e, exc_info=True)
        raise
    if w <= 0:
        logger.warning("_validate_window non-positive window %r", window)
        raise ValueError(f"invalid window {window!r}: must be >0")
    return w


def sma(series, window: int = 20, *args, **kwargs) -> pd.Series:
    """SMA 简单移动平均：rolling(window, min_periods=n).mean()，不足窗口为 NaN。"""
    # 兼容别名 n/period/span
    if args:
        pass  # 保留位置参数兼容位，当前无需额外处理
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
    # 滚动均值：需满窗口才出值，保持与 polars 版本一致
    return s.rolling(window=n, min_periods=n).mean()


def ema(series, span: int = 20, *args, **kwargs) -> pd.Series:
    """EMA 指数移动平均：ewm(span=n, adjust=False, min_periods=1).mean()。"""
    # 兼容别名 n/window/period
    if "n" in kwargs:
        span = kwargs["n"]
    if "window" in kwargs:
        span = kwargs["window"]
    if "period" in kwargs:
        span = kwargs["period"]
    n = _validate_window(span, default=20)
    s = _to_series(series)
    if s.empty:
        return s
    return s.ewm(span=n, adjust=False, min_periods=1).mean()  # Wilder 风格的指数平滑


def rsi(series, period: int = 14, *args, **kwargs) -> pd.Series:
    """RSI 相对强弱(0-100)：Wilder 平滑 ewm(alpha=1/n)，收益/损失分别指数平滑后求 RS。"""
    # 兼容别名 n/window/span
    if "n" in kwargs:
        period = kwargs["n"]
    if "window" in kwargs:
        period = kwargs["window"]
    if "span" in kwargs:
        period = kwargs["span"]
    n = _validate_window(period, default=14)
    s = _to_series(series)
    if s.empty:
        return pd.Series(dtype=float)
    # 窗口不足（有效数据点 < period+1，diff 后不足 period 个差分）时返回全 NaN，
    # 不得用 50.0 假中性误导策略（T1-PoC7；tool 层 compute_indicator 对 bars<window
    # 已有 ok:False，此处库层同样诚实）。
    if int(s.notna().sum()) < n + 1:
        return pd.Series([float("nan")] * len(s), index=s.index)
    # 涨跌分解：仅保留同向部分
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder 平滑：alpha = 1/n，指数加权而非简单滚动，避免滞后失真
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()

    rs = avg_gain / avg_loss  # 相对强度
    rsi_series = 100 - (100 / (1 + rs))  # 归一至 0-100

    # 边界：全为上涨则 RSI=100
    rsi_series = rsi_series.where(avg_loss != 0, 100.0)
    # 边界：横盘（无涨无跌）则中性 50
    flat_mask = (avg_gain == 0) & (avg_loss == 0)
    rsi_series = rsi_series.where(~flat_mask, 50.0)

    # 首元素等 NaN 回落至中性
    rsi_series = rsi_series.fillna(50.0)
    # 无穷保护
    rsi_series = rsi_series.replace([np.inf, -np.inf], np.nan).fillna(50.0)
    # 限幅保证有效区间
    rsi_series = rsi_series.clip(lower=0, upper=100)
    return rsi_series


def bollinger(series, window: int = 20, num_std: float = 2.0, *args, **kwargs):
    """布林带：middle=滚动均值，upper/lower=middle±k·滚动标准差（k 默认 2）。"""
    # 兼容别名 n/k/std/num_std
    if "n" in kwargs:
        window = kwargs["n"]
    if "k" in kwargs:
        num_std = kwargs["k"]
    if "std" in kwargs:
        num_std = kwargs["std"]
    if "num_std" in kwargs:
        num_std = kwargs["num_std"]

    n = _validate_window(window, default=20)
    try:
        k = float(num_std)
    except (ValueError, TypeError) as e:
        logger.warning("bollinger num_std parse failed %r: %s", num_std, e, exc_info=True)
        k = 2.0
    except Exception as e:
        logger.warning("bollinger num_std unexpected error %r: %s", num_std, e, exc_info=True)
        raise
    if k < 0:
        k = 2.0  # 负倍数无意义，回落默认

    s = _to_series(series)
    if s.empty:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    mid = s.rolling(window=n, min_periods=n).mean()
    # 滚动标准差 ddof=1 与 pandas 默认一致
    std = s.rolling(window=n, min_periods=n).std(ddof=1)
    # 带宽区间：不足窗口时保持 NaN，避免误导
    upper = mid + k * std
    lower = mid - k * std
    # 无穷保护
    mid = mid.replace([np.inf, -np.inf], np.nan)
    upper = upper.replace([np.inf, -np.inf], np.nan)
    lower = lower.replace([np.inf, -np.inf], np.nan)
    return mid, upper, lower


def macd(series, fast: int = 12, slow: int = 26, signal: int = 9, *args, **kwargs):
    """MACD：macd=EMA(fast)-EMA(slow)，signal=EMA(macd, signal)，hist=macd-signal。"""
    # 兼容别名 n_fast/n_slow/n_signal/window
    if "n_fast" in kwargs:
        fast = kwargs["n_fast"]
    if "n_slow" in kwargs:
        slow = kwargs["n_slow"]
    if "n_signal" in kwargs:
        signal = kwargs["n_signal"]
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
    macd_line = ema_fast - ema_slow  # 快慢线差
    signal_line = macd_line.ewm(span=signal_n, adjust=False, min_periods=1).mean()  # 信号线
    hist = macd_line - signal_line  # 柱状图

    # 无穷保护
    macd_line = macd_line.replace([np.inf, -np.inf], np.nan)
    signal_line = signal_line.replace([np.inf, -np.inf], np.nan)
    hist = hist.replace([np.inf, -np.inf], np.nan)

    return macd_line, signal_line, hist


def _extract_ohlc(high, low, close):
    """归一 high/low/close 三列：支持 DataFrame 单参或三序列分参，按位置对齐。

    DataFrame 须含 high/low/close 三列（大小写敏感）；三序列可为 Series/list/ndarray，
    经 _to_series 归一（非数值→NaN、Inf→NaN）。长度不一致抛 ValueError；索引取 close
    为主（Series/DataFrame 时保留其 index，否则取 high/low 的 index，都没有则 RangeIndex）。
    """
    if low is None and close is None:
        # 单参 DataFrame 形态：atr(df)
        if isinstance(high, pd.DataFrame):
            missing = [c for c in ("high", "low", "close") if c not in high.columns]
            if missing:
                raise ValueError(f"atr: DataFrame missing columns {missing}")
            h = _to_series(high["high"])
            ell = _to_series(high["low"])
            c = _to_series(high["close"])
            return h, ell, c
        raise ValueError("atr: requires high/low/close (or DataFrame with high/low/close columns)")
    if low is None or close is None:
        raise ValueError("atr: requires high, low and close together")
    h = _to_series(high)
    ell = _to_series(low)
    c = _to_series(close)
    if not (len(h) == len(ell) == len(c)):
        raise ValueError(f"atr: length mismatch high={len(h)} low={len(ell)} close={len(c)}")
    return h, ell, c


def atr(high, low=None, close=None, period: int = 14, *args, **kwargs) -> pd.Series:
    """ATR 平均真实波幅（Wilder）：TR=max(H-L, |H-prevC|, |L-prevC|)，ATR 为 Wilder 平滑。

    口径：TR[0]=H-L（无 prevC）；ATR[n-1]=mean(TR[0:n])，之后按
    ATR[i]=(ATR[i-1]*(n-1)+TR[i])/n 递推（与 TA-Lib Wilder 一致）。
    前 n-1 个为 NaN（不足窗口诚实置空，不填 0 冒充）；有效 TR 不足 n 个时全 NaN。
    NaN 的 TR 对应 ATR 为 NaN 且不参与平滑（状态沿用上一个有效 ATR）。
    """
    # 兼容别名 n/window/span
    if "n" in kwargs:
        period = kwargs["n"]
    if "window" in kwargs:
        period = kwargs["window"]
    if "span" in kwargs:
        period = kwargs["span"]
    n = _validate_window(period, default=14)
    h, ell, c = _extract_ohlc(high, low, close)

    # 索引：优先 close 的 Series/DataFrame 索引，保持与输入对齐
    idx = c.index
    if len(h) == 0:
        return pd.Series(dtype=float)

    h_arr = h.to_numpy(dtype=float)
    l_arr = ell.to_numpy(dtype=float)
    c_arr = c.to_numpy(dtype=float)

    import math as _math

    tr = np.full(len(c_arr), np.nan)
    for i in range(len(c_arr)):
        hi, li, ci = h_arr[i], l_arr[i], c_arr[i]
        if _math.isnan(hi) or _math.isnan(li) or _math.isnan(ci):
            continue  # 任一输入 NaN → TR NaN（诚实，不插值）
        hl = hi - li
        if i == 0 or _math.isnan(c_arr[i - 1]):
            tr[i] = hl
        else:
            pc = c_arr[i - 1]
            tr[i] = max(hl, abs(hi - pc), abs(li - pc))

    out = np.full(len(c_arr), np.nan)
    # Wilder 递推：跳过 NaN 的 TR（状态沿用），种子为前 n 个有效 TR 均值
    valid = [i for i, v in enumerate(tr) if not _math.isnan(v)]
    if len(valid) >= n:
        seed_idx = valid[n - 1]
        seed_vals = [tr[i] for i in valid[:n]]
        prev = float(sum(seed_vals) / n)
        out[seed_idx] = prev
        for i in range(seed_idx + 1, len(tr)):
            if _math.isnan(tr[i]):
                out[i] = np.nan  # 缺失 bar 置空，不消费状态
                continue
            prev = (prev * (n - 1) + float(tr[i])) / n
            out[i] = prev
    # Inf 保护（理论上 TR/均值不会产生 Inf，但保持与库内其他指标一致）
    res = pd.Series(out, index=idx, dtype=float)
    res = res.replace([np.inf, -np.inf], np.nan)
    res.name = "atr"
    return res


def max_drawdown(equity) -> float:
    """最大回撤：(equity/cummax-1).min()，空/全 NaN 回落 0。"""
    s = _to_series(equity)
    if s.empty:
        return 0.0
    # 剔除 NaN 后计算，若全 NaN 则无有效回撤
    s_clean = s.dropna()
    if s_clean.empty:
        return 0.0
    cummax = s_clean.cummax()  # 滚动峰值
    # 避免除零：cummax 为 0 时回撤置零（权益起点为 0 的边界）
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = s_clean / cummax - 1.0
    dd = dd.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    try:
        mdd = float(dd.min())  # 最深回撤
    except Exception:
        return 0.0
    if np.isnan(mdd) or np.isinf(mdd):
        return 0.0
    # 单调上涨时回撤为 0，不返回正值
    if mdd > 0:
        mdd = 0.0
    return mdd
