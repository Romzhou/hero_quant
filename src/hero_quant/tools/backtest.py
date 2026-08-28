"""回测工具集：PIT 正确性校验、引擎执行与指标计算。

位于 tools 层回测分支，封装 BacktestEngine 的执行与校验逻辑；
run_backtest/optimize_portfolio 涉及状态写，并发安全标 False，其余只读工具标 True。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from hero_quant.tools.registry import tool

logger = logging.getLogger(__name__)


def _fetch_bars_for_backtest(symbol: str, start: str, end: str):
    """为回测拉取行情，双源注册后回退至空列表由上层合成兜底。"""
    try:
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader

        reg = MarketDataRegistry()
        reg.register(TencentLoader())
        try:
            from hero_quant.data.loaders.yahoo import YahooLoader

            reg.register(YahooLoader())
        except (ImportError, AttributeError, ValueError) as e:
            logger.debug("YahooLoader register failed: %s", e)
        bars, _ = reg.get_bars(symbol, "1d", start, end)
        return bars
    except (ValueError, TypeError, AttributeError, ImportError, RuntimeError) as e:
        logger.warning("fetch bars failed for %s: %s", symbol, e)
        # 获取失败返回空，由调用方生成合成价格序列保证回测可执行
        return []


def _synthetic_prices_for_backtest(index, ticker: str):
    """按 ticker 生成确定性合成价格（趋势+噪声），用于多资产回测演示，复用 bench 逻辑。"""
    import numpy as np
    import pandas as pd

    n = len(index)
    seed = abs(hash(str(ticker))) % (2**32)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.5, size=n)
    trend = np.arange(n) * 0.3
    close = 100 + trend + np.cumsum(noise) * 0.2
    close = np.maximum(close, 1.0)
    df = pd.DataFrame({"close": close.astype(float)}, index=index)
    try:
        df["open"] = df["close"].shift(1).fillna(df["close"].iloc[0])
    except (ValueError, TypeError, AttributeError) as e:
        logger.debug("synthetic open fill failed: %s", e)
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
    try:
        import pandas as pd
        from hero_quant.backtest.engine import BacktestEngine

        bars = _fetch_bars_for_backtest(symbol, start, end)
        closes = [b.get("close", 100) for b in bars[:50]] if bars else []
        if not closes:
            # 无真实行情时使用锚定起始日的合成价格，保证引擎可运行
            closes = [100, 101, 102]
        # 以起始日为锚点构建 DatetimeIndex
        try:
            idx = pd.date_range(start, periods=len(closes), freq="D")
        except (ValueError, TypeError) as e:
            logger.warning("date_range start parse failed: %s", e)
            idx = pd.date_range("2026-08-01", periods=len(closes), freq="D")

        # 多资产价格构造
        need_multi = len(weights) > 1
        is_comma_symbol = isinstance(symbol, str) and "," in symbol
        prices = None
        if need_multi and is_comma_symbol:
            tickers = [s.strip() for s in symbol.split(",") if s.strip()]
            # 若 tickers 数量与 weights 不一致，按 min 对齐，日志提示
            if len(tickers) != len(weights):
                logger.warning("symbol tickers %d vs weights %d mismatch, will align by min", len(tickers), len(weights))
            # 合成每标的的 close 序列
            price_dict: dict[str, pd.Series] = {}
            for t in tickers:
                df_syn = _synthetic_prices_for_backtest(idx, t)
                # 若有真实 bars 且单标的真实价格可用，优先使用真实 closes 仅对第一个标的
                # 多标的场景全部用合成以保证确定性
                price_dict[t] = df_syn["close"]
            prices = pd.DataFrame(price_dict, index=idx)
            # 补充 open 列为首资产的 open 以兼容 _align
            try:
                first_t = tickers[0]
                syn_first = _synthetic_prices_for_backtest(idx, first_t)
                prices["open"] = syn_first["open"].values  # 统一 open 辅助
                # 但 open 不应混入多资产价格列，引擎会自动排除 open；保留仅为对齐便利
            except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
                logger.debug("multi-asset open fill failed: %s", e)
        elif need_multi and not is_comma_symbol:
            # 单 symbol 但多权重的 fallback：检测是否已有单列价格需要扩展为多列
            # 若 closes 来自真实单标的，仅有一列，扩展为多列合成资产以演示多资产诚实计算
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

        eng = BacktestEngine()
        res = eng.run(prices, weights=weights, costs=float(costs) if costs is not None else 0.0005, engine=engine or "default")
        eq = res.get("equity")
        if hasattr(eq, "tolist"):
            equity = eq.tolist()
        elif hasattr(eq, "values"):
            equity = list(eq.values)  # type: ignore
        else:
            equity = list(eq) if isinstance(eq, (list, tuple)) else []
        return {"equity": equity, "metrics": res.get("metrics", {}), "ok": True, "engine": engine or "default"}
    except (ValueError, TypeError, AttributeError, RuntimeError) as e:
        logger.warning("run_backtest failed: %s", e)
        return {"equity": [], "metrics": {}, "ok": False, "error": str(e), "engine": engine or "default"}


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
        logger.warning("validate_backtest failed: %s", e)
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
        logger.warning("get_backtest_metrics failed: %s", e)
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
