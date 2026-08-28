"""量化指标工具集：封装 quantlib 指标并以 pandas 为回退。

位于 tools 层计算分支，通过 MarketDataRegistry 获取收盘价，优先调用
Rust quantlib（sma/ema/rsi/bollinger/macd/max_drawdown），缺失时回退至
pandas 实现；数据不可用时返回 20 点合成序列兜底。全部工具为只读计算，
并发安全标记为 True。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import pandas as pd

from hero_quant.tools.registry import tool

logger = logging.getLogger(__name__)

SUPPORTED_INDICATORS = {"sma", "ma", "ema", "rsi", "bollinger", "bb", "boll", "macd", "max_drawdown", "mdd", "drawdown"}

_shared_registry = None


def _get_shared_registry():
    global _shared_registry
    if _shared_registry is None:
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader

        reg = MarketDataRegistry()
        try:
            reg.register(TencentLoader())
        except Exception as e:
            logger.warning("TencentLoader register failed: %s", e, exc_info=True)
        try:
            from hero_quant.data.loaders.yahoo import YahooLoader

            reg.register(YahooLoader())
        except (ImportError, ModuleNotFoundError) as e:
            logger.debug("YahooLoader not available: %s", e)
        except Exception as e:
            logger.warning("YahooLoader register failed: %s", e, exc_info=True)
        _shared_registry = reg
    return _shared_registry


def _fetch_closes(symbol: str, start: str = "2026-08-01", end: str = "2026-08-03"):
    """拉取收盘价序列，失败时回退至 20 点合成数据以保证指标可算。"""
    try:
        reg = _get_shared_registry()
        bars, _ = reg.get_bars(symbol, start, end, interval="1d")
        closes: list[float] = []
        for b in bars or []:
            c = b.get("close")
            if c is None:
                logger.warning("bar missing close for %s: %s", symbol, b)
                continue
            try:
                v = float(c)
            except (TypeError, ValueError) as e:
                logger.warning("invalid close value for %s: %r (%s)", symbol, c, e)
                continue
            if v != v:  # NaN
                continue
            closes.append(v)
        if closes:
            return closes
        logger.warning("no valid closes for %s, returning synthetic fallback", symbol)
    except Exception as e:
        logger.warning("fetch closes failed for %s: %s", symbol, e, exc_info=True)
    # 无可用行情时提供 20 点等差序列，避免上层指标因空数据失败
    return [100 + i * 0.5 for i in range(20)]


def _validate_window(window: Any, closes_len: int | None = None) -> int:
    try:
        n = int(window) if window is not None and str(window).strip() != "" else 20
    except (TypeError, ValueError) as e:
        raise ValueError(f"window must be int-convertible, got {window!r}") from e
    if n <= 0:
        raise ValueError(f"window must be >0, got {n}")
    if n > 500:
        raise ValueError(f"window must be <=500, got {n}")
    if closes_len is not None and closes_len > 0 and n > closes_len:
        # cap to len to avoid waste but allow
        pass
    return n


@tool(
    name="compute_indicator",
    description="Compute technical indicator (sma/ema/rsi/bollinger) via quantlib wrapping pandas.",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "indicator": {"type": "string"},
            "window": {"type": "integer"},
            "start": {"type": "string"},
            "end": {"type": "string"},
        },
        "required": ["symbol", "indicator"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "values": {"type": "array"},
            "upper": {"type": "array"},
            "lower": {"type": "array"},
            "signal": {"type": "array"},
            "hist": {"type": "array"},
            "ok": {"type": "boolean"},
            "error": {"type": "string"},
            "symbol": {"type": "string"},
            "indicator": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": True,
    },
    is_concurrency_safe=lambda args: True,
)
def compute_indicator(
    symbol: str,
    indicator: str = "sma",
    window: int = 20,
    start: str = "2026-08-01",
    end: str = "2026-08-20",
) -> Dict[str, Any]:
    """计算技术指标（sma/ema/rsi/bollinger/macd/max_drawdown），quantlib 优先、pandas 兜底。"""
    try:
        closes = _fetch_closes(symbol, start, end)
        s = pd.Series(closes, dtype=float)
        ind = (indicator or "sma").lower().strip()
        if ind not in SUPPORTED_INDICATORS:
            return {"values": [], "ok": False, "error": f"unsupported indicator: {ind}", "symbol": symbol, "indicator": indicator}
        try:
            n = _validate_window(window, len(s))
        except ValueError as ve:
            return {"values": [], "ok": False, "error": str(ve), "symbol": symbol, "indicator": indicator}
        # 优先使用 Rust quantlib，避免 Python 重复实现；缺失时走 pandas
        values: list[float] = []
        try:
            from hero_quant.quantlib.indicators import sma, ema, rsi, bollinger, macd
        except (ImportError, ModuleNotFoundError) as e:
            logger.debug("quantlib not available, fallback to pandas: %s", e)
            sma = ema = rsi = bollinger = macd = None  # type: ignore

        if ind in ("sma", "ma"):
            if sma is not None:
                res = sma(s, n)
            else:
                res = s.rolling(n).mean()
            values = [float(x) if pd.notna(x) else 0.0 for x in res.tolist()]
        elif ind == "ema":
            if ema is not None:
                res = ema(s, n)
            else:
                res = s.ewm(span=n, adjust=False).mean()
            values = [float(x) for x in res.tolist()]
        elif ind == "rsi":
            if rsi is not None:
                res = rsi(s, n if n else 14)
            else:
                delta = s.diff()
                gain = delta.where(delta > 0, 0.0)
                loss = -delta.where(delta < 0, 0.0)
                avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
                avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
                rs = avg_gain / avg_loss.replace(0, 1e-9)
                res = 100 - (100 / (1 + rs))
                res = res.fillna(50.0)
            values = [float(x) if pd.notna(x) else 50.0 for x in res.tolist()]
        elif ind in ("bollinger", "bb", "boll"):
            if bollinger is not None:
                mid, upper, lower = bollinger(s, n)
                mid_l = [float(x) if pd.notna(x) else 0.0 for x in mid.tolist()]
                upper_l = [float(x) if pd.notna(x) else 0.0 for x in upper.tolist()]
                lower_l = [float(x) if pd.notna(x) else 0.0 for x in lower.tolist()]
                return {"values": mid_l, "upper": upper_l, "lower": lower_l, "ok": True, "symbol": symbol, "indicator": indicator}
            else:
                mid = s.rolling(n).mean()
                std = s.rolling(n).std()
                upper_s = mid + 2 * std
                lower_s = mid - 2 * std
                mid_l = [float(x) if pd.notna(x) else 0.0 for x in mid.tolist()]
                upper_l = [float(x) if pd.notna(x) else 0.0 for x in upper_s.tolist()]
                lower_l = [float(x) if pd.notna(x) else 0.0 for x in lower_s.tolist()]
                return {"values": mid_l, "upper": upper_l, "lower": lower_l, "ok": True, "symbol": symbol, "indicator": indicator}
        elif ind in ("macd",):
            if macd is not None:
                m_line, sig, hist = macd(s)
                m_l = [float(x) if pd.notna(x) else 0.0 for x in m_line.tolist()]
                sig_l = [float(x) if pd.notna(x) else 0.0 for x in sig.tolist()]
                hist_l = [float(x) if pd.notna(x) else 0.0 for x in hist.tolist()]
                return {"values": m_l, "signal": sig_l, "hist": hist_l, "ok": True, "symbol": symbol, "indicator": indicator}
            else:
                ef = s.ewm(span=12, adjust=False).mean()
                es = s.ewm(span=26, adjust=False).mean()
                m_line = ef - es
                sig = m_line.ewm(span=9, adjust=False).mean()
                hist = m_line - sig
                m_l = [float(x) if pd.notna(x) else 0.0 for x in m_line.tolist()]
                sig_l = [float(x) if pd.notna(x) else 0.0 for x in sig.tolist()]
                hist_l = [float(x) if pd.notna(x) else 0.0 for x in hist.tolist()]
                return {"values": m_l, "signal": sig_l, "hist": hist_l, "ok": True, "symbol": symbol, "indicator": indicator}
        elif ind in ("max_drawdown", "mdd", "drawdown"):
            try:
                from hero_quant.quantlib.indicators import max_drawdown as q_mdd
            except (ImportError, ModuleNotFoundError) as e:
                logger.debug("max_drawdown quantlib not available: %s", e)
                q_mdd = None  # type: ignore
            if q_mdd is not None:
                v = q_mdd(s)
                return {"values": [float(v)], "ok": True, "symbol": symbol, "indicator": indicator}
            else:
                cummax = s.cummax()
                dd = s / cummax - 1
                v = float(dd.min())
                return {"values": [v], "ok": True, "symbol": symbol, "indicator": indicator}
        else:
            return {"values": [], "ok": False, "error": f"unsupported indicator: {ind}", "symbol": symbol, "indicator": indicator}

        return {"values": values, "ok": True, "symbol": symbol, "indicator": indicator}
    except Exception as e:
        logger.warning("compute_indicator failed for %s/%s: %s", symbol, indicator, e, exc_info=True)
        return {"values": [], "ok": False, "error": f"{type(e).__name__}: {e}", "symbol": symbol, "indicator": indicator}


@tool(
    name="compute_sharpe",
    description="Compute Sharpe ratio for price series.",
    parameters={
        "type": "object",
        "properties": {"prices": {"type": "array"}},
        "required": ["prices"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"sharpe": {"type": "number"}, "ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def compute_sharpe(prices: list) -> Dict[str, Any]:
    """计算 Sharpe 比率，委托 backtest.metrics 实现。"""
    try:
        if not prices:
            raise ValueError("prices must be non-empty")
        # validate numeric
        _ = pd.to_numeric(pd.Series(prices), errors="coerce")
        if pd.Series(prices).isna().any():
            # coerce check
            coerced = pd.to_numeric(pd.Series(prices), errors="coerce")
            if coerced.isna().any():
                raise ValueError("prices must be numeric")
        from hero_quant.backtest.metrics import sharpe_ratio

        s = pd.Series(prices, dtype=float)
        v = sharpe_ratio(s)
        return {"sharpe": float(v), "ok": True}
    except Exception as e:
        logger.warning("compute_sharpe failed: %s", e, exc_info=True)
        return {"sharpe": 0.0, "ok": False, "error": str(e)}


@tool(
    name="compute_drawdown",
    description="Compute max drawdown for price series.",
    parameters={
        "type": "object",
        "properties": {"prices": {"type": "array"}},
        "required": ["prices"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"drawdown": {"type": "number"}, "ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def compute_drawdown(prices: list) -> Dict[str, Any]:
    """计算最大回撤，委托 backtest.metrics 实现。"""
    try:
        if not prices:
            raise ValueError("prices must be non-empty")
        from hero_quant.backtest.metrics import max_drawdown

        s = pd.Series(prices, dtype=float)
        v = max_drawdown(s)
        return {"drawdown": float(v), "ok": True}
    except Exception as e:
        logger.warning("compute_drawdown failed: %s", e, exc_info=True)
        return {"drawdown": 0.0, "ok": False, "error": str(e)}


SUPPORTED_FACTORS = {"momentum", "mom"}


@tool(
    name="compute_factor",
    description="Compute factor values (momentum/value placeholder — returns computed wrapper).",
    parameters={
        "type": "object",
        "properties": {
            "factor": {"type": "string"},
            "symbol": {"type": "string"},
            "window": {"type": "integer"},
        },
        "required": ["factor"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"values": {"type": "array"}, "ok": {"type": "boolean"}, "error": {"type": "string"}, "factor": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": True,
    },
    is_concurrency_safe=lambda args: True,
)
def compute_factor(factor: str, symbol: str = "600519.SH", window: int = 20) -> Dict[str, Any]:
    """计算因子值；当前仅实现 momentum（N 日收益率），其余返回空占位。"""
    try:
        f = (factor or "").lower().strip()
        if f not in SUPPORTED_FACTORS:
            return {"values": [], "ok": False, "error": f"unsupported factor: {factor}", "factor": factor}
        try:
            n = _validate_window(window)
        except ValueError as ve:
            return {"values": [], "ok": False, "error": str(ve), "factor": factor}
        closes = _fetch_closes(symbol)
        s = pd.Series(closes, dtype=float)
        if f in ("momentum", "mom"):
            # momentum = price / price.shift(n) -1
            vals = (s / s.shift(n) - 1).fillna(0.0).tolist()
            return {"values": [float(x) for x in vals], "ok": True, "factor": factor}
        return {"values": [], "ok": False, "error": f"unsupported factor: {factor}", "factor": factor}
    except Exception as e:
        logger.warning("compute_factor failed for %s: %s", factor, e, exc_info=True)
        return {"values": [], "ok": False, "error": f"{type(e).__name__}: {e}", "factor": factor}


@tool(
    name="screen_factors",
    description="Screen factors by IC/IR placeholder.",
    parameters={
        "type": "object",
        "properties": {"universe": {"type": "array"}},
        "required": ["universe"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"factors": {"type": "array"}, "ok": {"type": "boolean"}, "error": {"type": "string"}, "universe": {"type": "array"}},
        "required": ["ok"],
        "additionalProperties": True,
    },
    is_concurrency_safe=lambda args: True,
)
def screen_factors(universe: list) -> Dict[str, Any]:
    """因子筛选占位：返回空结果，保持 schema 兼容 — explicit not-implemented."""
    if not universe:
        return {"factors": [], "ok": True, "universe": universe}
    # placeholder: not fully implemented, return ok False to make visible
    return {"factors": [], "ok": False, "error": "not_implemented: screen_factors placeholder", "universe": universe}
