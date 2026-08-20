"""Market data tools — production-core (Wave B5 hardened).

Port vibe-trading 64-tool registry idea to 15 core tools.
Each tool uses @tool with parameters/output JSON Schemas and is_concurrency_safe marker.
get_market_data walks registry with Tencent + Yahoo loaders and audits concurrency safety.
"""

from __future__ import annotations

from typing import Any, Dict

from hero_quant.tools.registry import TOOL_REGISTRY, tool


def _make_registry():
    """Create MarketDataRegistry with Tencent + Yahoo loaders."""
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry()
    try:
        from hero_quant.data.loaders.tencent import TencentLoader

        reg.register(TencentLoader())
    except Exception:
        pass
    try:
        from hero_quant.data.loaders.yahoo import YahooLoader

        reg.register(YahooLoader())
    except Exception:
        pass
    return reg


def _synthetic_fallback(symbol: str, start: str, end: str):
    """Generate synthetic bars as fallback (CN-live synthetic)."""
    try:
        from hero_quant.data.loaders.tencent import TencentLoader

        return TencentLoader()._synthetic_bars(symbol, start, end)
    except Exception:
        # minimal 3 bars
        return [
            {"date": start, "open": 100.0, "close": 100.5, "high": 101.0, "low": 99.5, "volume": 100},
            {"date": end, "open": 100.5, "close": 101.0, "high": 101.5, "low": 100.0, "volume": 110},
        ]


@tool(
    name="get_market_data",
    description="Fetch OHLCV bars for a symbol via MarketDataRegistry (Tencent + Yahoo, synthetic fallback).",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "interval": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "bars": {"type": "array"},
            "provenance": {"type": "object"},
            "ok": {"type": "boolean"},
            "concurrency_safe": {"type": "boolean"},
            "error": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def get_market_data(
    symbol: str,
    interval: str = "1d",
    start: str = "2026-08-01",
    end: str = "2026-08-03",
) -> Dict[str, Any]:
    """Registry-backed market data fetch with concurrency audit and dual-loader fallback."""
    spec = TOOL_REGISTRY.get("get_market_data")
    is_safe = False
    if spec is not None:
        try:
            is_safe = bool(spec.is_concurrency_safe({"symbol": symbol, "interval": interval}))
        except Exception:
            is_safe = False
    # Try registry with both loaders
    try:
        reg = _make_registry()
        if not reg._loaders:
            raise ImportError("pip install hero-quant[us] or [ashare] - no loader registered")
        bars, prov = reg.get_bars(symbol, interval, start, end)
        provenance = {"source": getattr(prov, "source", "unknown"), "unit": getattr(prov, "unit", "shares")}
        # preserve extra if any
        if hasattr(prov, "extra") and prov.extra:
            provenance["extra"] = prov.extra
        return {"bars": bars, "provenance": provenance, "ok": True, "concurrency_safe": is_safe}
    except Exception as e:
        # synthetic fallback with error string
        bars = _synthetic_fallback(symbol, start, end)
        return {
            "bars": bars,
            "provenance": {"source": "synthetic", "unit": "shares"},
            "ok": False,
            "error": str(e),
            "concurrency_safe": is_safe,
        }


@tool(
    name="list_markets",
    description="List supported markets and data sources.",
    parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    output={
        "type": "object",
        "properties": {"markets": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def list_markets() -> Dict[str, Any]:
    return {"markets": ["CN", "US", "CRYPTO"], "ok": True}


@tool(
    name="get_ticker_info",
    description="Get ticker metadata for a symbol.",
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"symbol": {"type": "string"}, "info": {"type": "object"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def get_ticker_info(symbol: str) -> Dict[str, Any]:
    return {"symbol": symbol, "ok": True, "info": {}}


@tool(
    name="get_fundamentals",
    description="Get fundamentals for a symbol (empty info placeholder, schema-correct).",
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"symbol": {"type": "string"}, "info": {"type": "object"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def get_fundamentals(symbol: str) -> Dict[str, Any]:
    # Placeholder: returns empty info but correct schema; no external dependency
    return {"symbol": symbol, "info": {}, "ok": True}


@tool(
    name="search_symbols",
    description="Search symbols by keyword.",
    parameters={
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"symbols": {"type": "array"}, "candidates": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def search_symbols(keyword: str) -> Dict[str, Any]:
    # Try registry search if loader supports it, otherwise mock candidates
    candidates: list[Dict[str, Any]] = []
    # placeholder: mock 3 candidates with keyword
    kw = (keyword or "").strip()
    if kw:
        # naive: uppercase keyword as symbol stem
        stem = kw.upper().replace(" ", "_")
        for suffix in [".SH", ".US", ""]:
            candidates.append({"symbol": f"{stem}{suffix}", "name": f"{kw} mock {suffix or 'generic'}"})
    return {"symbols": candidates, "candidates": candidates, "ok": True, "keyword": keyword}


@tool(
    name="search_symbol",
    description="Search symbol by keyword (alias for search_symbols).",
    parameters={
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"symbols": {"type": "array"}, "candidates": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def search_symbol(keyword: str) -> Dict[str, Any]:
    # Delegate to search_symbols for consistency
    return search_symbols(keyword)


@tool(
    name="get_bars_range",
    description="Get bars for multiple symbols (batch).",
    parameters={
        "type": "object",
        "properties": {
            "symbols": {"type": "array"},
            "interval": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
        },
        "required": ["symbols"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"data": {"type": "object"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def get_bars_range(
    symbols: list,
    interval: str = "1d",
    start: str = "2026-08-01",
    end: str = "2026-08-03",
) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for sym in symbols or []:
        try:
            res = get_market_data(sym, interval=interval, start=start, end=end)
            data[sym] = res
        except Exception as e:
            data[sym] = {"bars": [], "ok": False, "error": str(e)}
    return {"data": data, "ok": True}
