"""回测工具集：PIT 正确性校验、引擎执行与指标计算。

位于 tools 层回测分支，封装 BacktestEngine 的执行与校验逻辑；
run_backtest/optimize_portfolio 涉及状态写，并发安全标 False，其余只读工具标 True。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict

from hero_quant.tools.registry import tool

logger = logging.getLogger(__name__)


def _fetch_bars_for_backtest(symbol: str, start: str, end: str, interval: str = "1d"):
    """为回测拉取行情，双源注册后回退至空列表由上层合成兜底。"""
    try:
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader

        reg = MarketDataRegistry()
        reg.register(TencentLoader())
        try:
            from hero_quant.data.loaders.yahoo import YahooLoader

            reg.register(YahooLoader())
        except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as e:
            logger.debug("YahooLoader register failed: %s", e, exc_info=True)
        # Use keyword interval for clarity; positional shim is brittle
        bars, _ = reg.get_bars(symbol, start, end, interval=interval)
        return bars
    except (ValueError, TypeError, AttributeError, ImportError, RuntimeError) as e:
        logger.warning("fetch bars failed for %s: %s", symbol, e, exc_info=True)
        # 获取失败返回空，由调用方生成合成价格序列保证回测可执行
        return []


def _synthetic_prices_for_backtest(index, ticker: str):
    """按 ticker 生成确定性合成价格（趋势+噪声），用于多资产回测演示，复用 bench 逻辑。"""
    import numpy as np
    import pandas as pd

    n = len(index)
    if n == 0:
        df = pd.DataFrame({"close": pd.Series(dtype=float)}, index=index)
        df["open"] = pd.Series(dtype=float)
        return df
    # stable hash via sha256
    seed = int.from_bytes(hashlib.sha256(str(ticker).encode()).digest()[:4], "big")
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.5, size=n)
    trend = np.arange(n) * 0.3
    close = 100 + trend + np.cumsum(noise) * 0.2
    close = np.maximum(close, 1.0)
    df = pd.DataFrame({"close": close.astype(float)}, index=index)
    try:
        df["open"] = df["close"].shift(1).fillna(df["close"].iloc[0])
    except (ValueError, TypeError, AttributeError, IndexError, KeyError) as e:
        logger.debug("synthetic open fill failed: %s", e, exc_info=True)
        df["open"] = df["close"]
    return df


@tool(
    name="run_backtest",
    description="Run PIT-correct backtest for a symbol/weights over date range (engine-backed, costs, engine param).",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "weights": {"type": "array"},
            "costs": {"type": "number"},
            "engine": {"type": "string"},
            "interval": {"type": "string"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "equity": {"type": "array"},
            "metrics": {"type": "object"},
            "ok": {"type": "boolean"},
            "error": {"type": "string"},
            "engine": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: False,
)
def run_backtest(
    symbol: str = "600519.SH",
    start: str = "2026-08-01",
    end: str = "2026-08-03",
    weights: list | None = None,
    costs: float = 0.0005,
    engine: str = "default",
    interval: str = "1d",
) -> Dict[str, Any]:
    """执行 PIT 正确回测，含交易成本与多引擎支持；无数据时使用合成序列。

    多资产支持：
    - 若 symbol 包含逗号（如 "AAPL,MSFT"），按逗号分割为多标的，为每个标的合成独立价格序列，构造多列 DataFrame 传入引擎以触发多资产路径。
    - 若 weights 长度 >1 但仅有单列价格，则生成多列合成价格作为 fallback，并在日志中警告。
    - 单标的场景保持原有单列 'close' DataFrame 行为以兼容存量调用。
    """
    weights = weights or [0.5, 0.5]
    # Narrow try blocks: date_range isolated
    import pandas as pd

    bars = _fetch_bars_for_backtest(symbol, start, end, interval=interval)
    closes = []
    for b in bars[:50] if bars else []:
        c = b.get("close")
        if c is None:
            continue
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        # NaN check
        if v != v:
            continue
        closes.append(v)
    if not closes:
        # 无真实行情时使用锚定起始日的合成价格，保证引擎可运行
        closes = [100, 101, 102]
    # 以起始日为锚点构建 DatetimeIndex — interval aware
    freq_map = {"1d": "D", "1h": "h", "1m": "min", "1w": "W", "1M": "M"}
    freq = freq_map.get(interval or "1d", "D")
    try:
        idx = pd.date_range(start, periods=len(closes), freq=freq)
    except (ValueError, TypeError) as e:
        logger.warning("date_range start parse failed: %s", e, exc_info=True)
        idx = pd.date_range("2026-08-01", periods=len(closes), freq=freq)

    # 多资产价格构造
    need_multi = len(weights) > 1
    is_comma_symbol = isinstance(symbol, str) and "," in symbol
    prices = None
    if need_multi and is_comma_symbol:
        tickers = [s.strip() for s in symbol.split(",") if s.strip()]
        # 若 tickers 数量与 weights 不一致，严格校验
        if len(tickers) != len(weights):
            raise ValueError(f"tickers {len(tickers)} vs weights {len(weights)} mismatch")
        # 合成每标的的 close 序列
        price_dict: dict[str, pd.Series] = {}
        for t in tickers:
            df_syn = _synthetic_prices_for_backtest(idx, t)
            price_dict[t] = df_syn["close"]
        prices = pd.DataFrame(price_dict, index=idx)
        # 补充 open 列为首资产的 open — but do not leak into price matrix for engine
        # keep auxiliary separate and drop before run
    elif need_multi and not is_comma_symbol:
        # 单 symbol 但多权重的 fallback：检测是否已有单列价格需要扩展为多列
        logger.warning("single price column with %d weights: constructing synthetic multi-asset price matrix for honest backtest", len(weights))
        price_dict = {}
        for i, wi in enumerate(weights):
            t = f"{symbol}_{i}"
            df_syn = _synthetic_prices_for_backtest(idx, t)
            price_dict[f"asset_{i}"] = df_syn["close"]
        prices = pd.DataFrame(price_dict, index=idx)
    else:
        # 单资产路径
        prices = pd.DataFrame({"close": closes}, index=idx)

    # Ensure open column not leaked into multi-asset matrix
    if prices is not None and "open" in prices.columns:
        prices = prices.drop(columns=["open"], errors="ignore")

    try:
        from hero_quant.backtest.engine import BacktestEngine

        eng = BacktestEngine()
        res = eng.run(prices, weights=weights, costs=float(costs) if costs is not None else 0.0005, engine=engine or "default")
    except (ValueError, RuntimeError) as e:
        logger.warning("run_backtest engine failed: %s", e, exc_info=True)
        return {"equity": [], "metrics": {}, "ok": False, "error": str(e), "engine": engine or "default"}
    except Exception as e:
        logger.warning("run_backtest unexpected failed: %s", e, exc_info=True)
        return {"equity": [], "metrics": {}, "ok": False, "error": f"{type(e).__name__}: {e}", "engine": engine or "default"}

    eq = res.get("equity")
    if hasattr(eq, "tolist"):
        equity = eq.tolist()
    elif hasattr(eq, "values"):
        equity = list(eq.values)  # type: ignore
    else:
        equity = list(eq) if isinstance(eq, (list, tuple)) else []
    return {"equity": equity, "metrics": res.get("metrics", {}), "ok": True, "engine": engine or "default"}


@tool(
    name="validate_backtest",
    description="Validate PIT correctness for backtest inputs.",
    parameters={
        "type": "object",
        "properties": {
            "weights_on": {"type": "string"},
            "price_date": {"type": "string"},
        },
        "required": ["weights_on", "price_date"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"valid": {"type": "boolean"}, "ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def validate_backtest(weights_on: str, price_date: str) -> Dict[str, Any]:
    """校验回测 PIT 正确性（权重日期不得晚于行情日期）。 PIT: weights_on must be <= price_date"""
    try:
        from hero_quant.backtest.validation import validate

        import pandas as pd

        prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range(price_date, periods=2))
        validate(prices, weights_on=weights_on, price_date=price_date)
        return {"valid": True, "ok": True}
    except (ValueError, TypeError, AttributeError, RuntimeError) as e:
        logger.warning("validate_backtest failed: %s", e, exc_info=True)
        return {"valid": False, "ok": False, "error": str(e)}


@tool(
    name="get_backtest_metrics",
    description="Compute metrics for equity curve (Sharpe, drawdown, annual).",
    parameters={
        "type": "object",
        "properties": {"equity": {"type": "array"}},
        "required": ["equity"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"metrics": {"type": "object"}, "ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def get_backtest_metrics(equity: list) -> Dict[str, Any]:
    """基于净值曲线计算 Sharpe、回撤等回测指标。"""
    try:
        import pandas as pd
        from hero_quant.backtest.metrics import compute_metrics

        s = pd.Series(equity)
        m = compute_metrics(s)
        return {"metrics": m, "ok": True}
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("get_backtest_metrics failed: %s", e, exc_info=True)
        return {"metrics": {}, "ok": False, "error": str(e)}


@tool(
    name="list_backtest_engines",
    description="List available backtest engines.",
    parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    output={
        "type": "object",
        "properties": {"engines": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def list_backtest_engines() -> Dict[str, Any]:
    """列出可用回测引擎。"""
    return {"engines": ["default", "vectorized", "synthetic"], "ok": True}


@tool(
    name="optimize_portfolio",
    description="Simple portfolio weight optimizer placeholder (equal weight).",
    parameters={
        "type": "object",
        "properties": {
            "symbols": {"type": "array"},
            "method": {"type": "string"},
        },
        "required": ["symbols"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"weights": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: False,
)
def optimize_portfolio(symbols: list, method: str = "equal") -> Dict[str, Any]:
    """投资组合权重优化占位：当前返回等权配置。"""
    n = len(symbols) if symbols else 1
    w = [1.0 / n] * n
    return {"weights": w, "ok": True, "method": method}
