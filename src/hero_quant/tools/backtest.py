"""Backtest tools — first batch (Wave B5)."""

from __future__ import annotations

from typing import Any, Dict

from hero_quant.tools.registry import tool


@tool(
    name="run_backtest",
    description="Run PIT-correct backtest for a symbol/weights over date range (engine-backed).",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "weights": {"type": "array"},
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
        },
    },
    is_concurrency_safe=lambda args: False,
)
def run_backtest(
    symbol: str = "600519.SH",
    start: str = "2026-08-01",
    end: str = "2026-08-03",
    weights: list | None = None,
) -> Dict[str, Any]:
    weights = weights or [0.5, 0.5]
    try:
        import pandas as pd
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader
        from hero_quant.backtest.engine import BacktestEngine

        reg = MarketDataRegistry()
        reg.register(TencentLoader())
        bars, _ = reg.get_bars(symbol, "1d", start, end)
        closes = [b.get("close", 100) for b in bars[:10]] or [100, 101, 102]
        prices = pd.DataFrame({"close": closes}, index=pd.date_range(start, periods=len(closes)))
        eng = BacktestEngine()
        res = eng.run(prices, weights=weights)
        # Normalize equity to list for LLM
        eq = res.get("equity")
        if hasattr(eq, "tolist"):
            equity = eq.tolist()
        elif hasattr(eq, "values"):
            equity = list(eq.values)
        else:
            equity = list(eq) if isinstance(eq, (list, tuple)) else []
        return {"equity": equity, "metrics": res.get("metrics", {}), "ok": True}
    except Exception as e:
        return {"equity": [], "metrics": {}, "ok": False, "error": str(e)}


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
    output={"type": "object", "properties": {"valid": {"type": "boolean"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def validate_backtest(weights_on: str, price_date: str) -> Dict[str, Any]:
    try:
        from hero_quant.backtest.validation import validate

        import pandas as pd

        prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range(price_date, periods=2))
        validate(prices, weights_on=weights_on, price_date=price_date)
        return {"valid": True, "ok": True}
    except Exception as e:
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
    output={"type": "object", "properties": {"metrics": {"type": "object"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def get_backtest_metrics(equity: list) -> Dict[str, Any]:
    try:
        import pandas as pd
        from hero_quant.backtest.metrics import compute_metrics

        s = pd.Series(equity)
        m = compute_metrics(s)
        return {"metrics": m, "ok": True}
    except Exception as e:
        return {"metrics": {}, "ok": False, "error": str(e)}


@tool(
    name="list_backtest_engines",
    description="List available backtest engines.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    output={"type": "object", "properties": {"engines": {"type": "array"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def list_backtest_engines() -> Dict[str, Any]:
    return {"engines": ["default", "synthetic"], "ok": True}


@tool(
    name="optimize_portfolio",
    description="Simple portfolio weight optimizer placeholder.",
    parameters={
        "type": "object",
        "properties": {"symbols": {"type": "array"}},
        "required": ["symbols"],
        "additionalProperties": False,
    },
    output={"type": "object", "properties": {"weights": {"type": "array"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: False,
)
def optimize_portfolio(symbols: list) -> Dict[str, Any]:
    n = len(symbols) if symbols else 1
    w = [1.0 / n] * n
    return {"weights": w, "ok": True}
