"""QuantLib tools — first batch (Wave B5)."""

from __future__ import annotations

from typing import Any, Dict

from hero_quant.tools.registry import tool


@tool(
    name="compute_indicator",
    description="Compute technical indicator (MA/RSI placeholder).",
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string"}, "indicator": {"type": "string"}},
        "required": ["symbol", "indicator"],
        "additionalProperties": False,
    },
    output={"type": "object", "properties": {"values": {"type": "array"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def compute_indicator(symbol: str, indicator: str = "ma") -> Dict[str, Any]:
    return {"values": [], "ok": True, "symbol": symbol, "indicator": indicator}


@tool(
    name="compute_sharpe",
    description="Compute Sharpe ratio for price series.",
    parameters={
        "type": "object",
        "properties": {"prices": {"type": "array"}},
        "required": ["prices"],
        "additionalProperties": False,
    },
    output={"type": "object", "properties": {"sharpe": {"type": "number"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def compute_sharpe(prices: list) -> Dict[str, Any]:
    try:
        import pandas as pd
        from hero_quant.backtest.metrics import sharpe_ratio

        s = pd.Series(prices)
        v = sharpe_ratio(s)
        return {"sharpe": float(v), "ok": True}
    except Exception as e:
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
    output={"type": "object", "properties": {"drawdown": {"type": "number"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def compute_drawdown(prices: list) -> Dict[str, Any]:
    try:
        import pandas as pd
        from hero_quant.backtest.metrics import max_drawdown

        s = pd.Series(prices)
        v = max_drawdown(s)
        return {"drawdown": float(v), "ok": True}
    except Exception as e:
        return {"drawdown": 0.0, "ok": False, "error": str(e)}


@tool(
    name="compute_factor",
    description="Compute factor values (momentum/value placeholder).",
    parameters={
        "type": "object",
        "properties": {"factor": {"type": "string"}},
        "required": ["factor"],
        "additionalProperties": False,
    },
    output={"type": "object", "properties": {"values": {"type": "array"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def compute_factor(factor: str) -> Dict[str, Any]:
    return {"values": [], "ok": True, "factor": factor}


@tool(
    name="screen_factors",
    description="Screen factors by IC/IR placeholder.",
    parameters={
        "type": "object",
        "properties": {"universe": {"type": "array"}},
        "required": ["universe"],
        "additionalProperties": False,
    },
    output={"type": "object", "properties": {"factors": {"type": "array"}, "ok": {"type": "boolean"}}},
    is_concurrency_safe=lambda args: True,
)
def screen_factors(universe: list) -> Dict[str, Any]:
    return {"factors": [], "ok": True, "universe": universe}
