"""行情工具集：提供 OHLCV 拉取、标的搜索等只读工具。

位于 tools 层数据入口，基于 MarketDataRegistry 聚合 Tencent（CN, board_lots）
与 Yahoo（US, shares）双源，通过 provenance{source, unit} 全链路记录数据
来源与单位；缺省回退至合成数据。并发安全上读操作标 True，写操作标 False。
"""

from __future__ import annotations

from typing import Any, Dict

from hero_quant.tools.registry import TOOL_REGISTRY, tool


def _make_registry():
    """创建已注册 Tencent + Yahoo 的 MarketDataRegistry（双源 fallback 链）。"""
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
    """合成兜底：复用 TencentLoader 的合成逻辑，保证离线可运行。"""
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
    """通过 Registry 拉取行情，含并发安全审计与双源回退；失败回退合成数据。"""
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
        # 透传 provenance 额外字段，便于上游追踪来源细节
        if hasattr(prov, "extra") and prov.extra:
            provenance["extra"] = prov.extra
        return {"bars": bars, "provenance": provenance, "ok": True, "concurrency_safe": is_safe}
    except Exception as e:
        # 异常时仍返回合成数据并附带错误信息，避免 Agent 中断
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
    """列出支持的市场与数据源（CN/US/CRYPTO）。"""
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
    """获取标的基础信息（占位实现，保持 schema 兼容）。"""
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
    """获取基本面信息（占位实现，无外部依赖，保持 schema 正确）。"""
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
    """按关键字搜索标的，返回模拟候选（保持离线可用）。"""
    candidates: list[Dict[str, Any]] = []
    # 离线环境下以关键字派生模拟候选，避免依赖外部搜索接口

    kw = (keyword or "").strip()
    if kw:
        # 将关键字大写作为标的前缀，拼接常见后缀生成候选

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
    """search_symbols 的别名，保持工具命名兼容。"""
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
    """批量拉取多标的行情，逐个调用 get_market_data 并聚合结果。"""
    data: Dict[str, Any] = {}
    for sym in symbols or []:
        try:
            res = get_market_data(sym, interval=interval, start=start, end=end)
            data[sym] = res
        except Exception as e:
            data[sym] = {"bars": [], "ok": False, "error": str(e)}
    return {"data": data, "ok": True}
