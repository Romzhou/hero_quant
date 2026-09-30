"""行情工具集：提供 OHLCV 拉取、标的搜索等只读工具。

位于 tools 层数据入口，基于 MarketDataRegistry 聚合 Tencent（CN, board_lots）
与 Yahoo（US, shares）双源，通过 provenance{source, unit} 全链路记录数据
来源与单位；缺省回退至合成数据。并发安全上读操作标 True，写操作标 False。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from hero_quant.tools.registry import TOOL_REGISTRY, tool

_logger = logging.getLogger(__name__)

_shared_registry = None
_shared_lock = threading.RLock()  # 中文：保护 _shared_registry 的 check-then-act，避免并发重复初始化


def _make_registry():
    """创建已注册 Tencent + Yahoo 的 MarketDataRegistry（双源 fallback 链）。"""
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry()
    try:
        from hero_quant.data.loaders.tencent import TencentLoader

        reg.register(TencentLoader())
    except (ImportError, ValueError, TypeError, OSError, RuntimeError) as e:  # 中文：窄化捕获，避免宽 except 吞没校验错
        _logger.warning("failed to register TencentLoader: %s", e, exc_info=True)
    try:
        from hero_quant.data.loaders.yahoo import YahooLoader

        reg.register(YahooLoader())
    except (ImportError, ValueError, TypeError, OSError, RuntimeError) as e:  # 中文：窄化捕获
        _logger.warning("failed to register YahooLoader: %s", e, exc_info=True)
    return reg


def _get_shared_registry():
    """线程安全获取共享 registry（双重检查 + RLock）。"""
    global _shared_registry
    if _shared_registry is None:
        with _shared_lock:
            if _shared_registry is None:
                _shared_registry = _make_registry()
    return _shared_registry


def _synthetic_fallback(symbol: str, start: str, end: str):
    """合成兜底：优先使用公开 generate_synthetic_bars，否则本地最小合成，保证离线可运行。"""
    try:
        from hero_quant.data.loaders.tencent import generate_synthetic_bars  # type: ignore

        return generate_synthetic_bars(symbol, start, end)
    except (ImportError, AttributeError, OSError, RuntimeError) as e:  # 中文：窄化捕获
        _logger.debug("public synthetic helper not available: %s", e, exc_info=True)
    # 本地最小合成前先校验日期 — validation errors must fail closed, never
    # propagate invalid dates as if they were data
    try:
        import pandas as _pd

        _pd.to_datetime(start)
        _pd.to_datetime(end)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid start/end for synthetic fallback: {e}") from e
    # 本地最小合成 — 直接返回字面量（构造不可能抛，避免无效包裹）
    return [
        {"date": start, "open": 100.0, "close": 100.5, "high": 101.0, "low": 99.5, "volume": 100},
        {"date": end, "open": 100.5, "close": 101.0, "high": 101.5, "low": 100.0, "volume": 110},
    ]


def _provenance_dict(prov) -> dict:
    """provenance 强校验导出：source 非空 str + unit∈{board_lots,shares}，补 adjust/factor_asof。

    中文：缺 unit 直接抛（CN 手/股 100x 口径，不做 shares 静默默认）；
    adjust 缺省 'unknown'（tencent/akshare live 经 registry 补为 qfq）。
    与 agent.grounding 的冻结 provenance schema 对齐。
    """
    source = getattr(prov, "source", None)
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"provenance.source must be non-empty str, got {source!r} (fail-closed)")
    unit = getattr(prov, "unit", None)
    if unit not in ("board_lots", "shares"):
        raise ValueError(
            f"provenance.unit must be 'board_lots' or 'shares', got {unit!r} (fail-closed, no silent default)"
        )
    out = {"source": source.strip(), "unit": unit}
    adjust = getattr(prov, "adjust", None) or "unknown"
    out["adjust"] = adjust
    try:
        asof = getattr(prov, "factor_asof", None)
    except (AttributeError, TypeError, ValueError):
        asof = None
    if asof is not None:
        out["factor_asof"] = asof
    # 透传 provenance 额外字段，便于上游追踪来源细节
    try:
        extra = getattr(prov, "extra", None)
    except (AttributeError, TypeError, ValueError):
        extra = None
    if extra:
        out["extra"] = extra
    return out


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
            "allow_synthetic": {"type": "boolean"},
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
    allow_synthetic: bool = True,
) -> Dict[str, Any]:
    """通过 Registry 拉取行情，含并发安全审计与双源回退；合成需显式 allow_synthetic=True。

    中文：合成自动回退仅靠 ok:False 标记不阻断——调用方忽略 ok 仍会把合成当 live
    用。修复后瞬时/网络错误默认直接抛错，仅当显式 allow_synthetic=True 才回退
    合成（且仍标记 ok:False + provenance synthetic）。
    """
    spec = TOOL_REGISTRY.get("get_market_data")
    is_safe = False
    if spec is not None:
        try:
            is_safe = bool(spec.is_concurrency_safe({"symbol": symbol, "interval": interval}))
        except (AttributeError, TypeError, ValueError, RuntimeError) as e:  # 中文：窄化捕获
            _logger.debug("is_concurrency_safe check failed: %s", e, exc_info=True)
            is_safe = False
    # 复用共享 registry，避免每调用重建
    try:
        reg = _get_shared_registry()
        # MarketDataRegistry.get_bars 已在无 loader 时抛 ImportError，无需 len(reg) 私有探测
        bars, prov = reg.get_bars(symbol, start, end, interval=interval)
        provenance = _provenance_dict(prov)
        return {"bars": bars, "provenance": provenance, "ok": True, "concurrency_safe": is_safe}
    except Exception as e:  # 中文：集中分发，需窄化后再决定合成或透传
        from hero_quant.data.registry import CrossSourceError as _CSE

        if isinstance(e, _CSE):
            _logger.warning("cross_source check blocked get_market_data for %s: %s", symbol, e, exc_info=True)
            raise
        if isinstance(e, (ValueError, TypeError)):
            # 校验类错误 fail-closed，不得合成冒充 live
            _logger.warning("get_market_data validation failed for %s: %s", symbol, e, exc_info=True)
            raise
        if isinstance(e, ImportError):
            raise RuntimeError("market data misconfigured: no loader available") from e
        if isinstance(e, (TimeoutError, ConnectionError, OSError, RuntimeError)):
            # 合成回退需显式 allow_synthetic=True：默认直接抛，防调用方忽略 ok 拿合成当 live
            if not allow_synthetic:
                _logger.warning(
                    "get_market_data refused synthetic fallback for %s (allow_synthetic=False): %s",
                    symbol, e, exc_info=True,
                )
                raise RuntimeError(
                    f"market data unavailable for {symbol}: {e} (synthetic fallback requires allow_synthetic=True)"
                ) from e
            # 显式 opt-in 后才回退合成，且标记 ok:False + provenance synthetic 不可用作 live
            _logger.warning("get_market_data fallback to synthetic for %s: %s", symbol, e, exc_info=True)
            bars = _synthetic_fallback(symbol, start, end)
            return {
                "bars": bars,
                "provenance": {"source": "synthetic", "unit": "shares", "adjust": "none"},
                "ok": False,
                "error": str(e),
                "concurrency_safe": is_safe,
            }
        raise


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
    return {"symbols": candidates, "candidates": candidates, "ok": True}


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
            "allow_synthetic": {"type": "boolean"},
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
    allow_synthetic: bool = True,
) -> Dict[str, Any]:
    """批量拉取多标的行情，逐个调用 get_market_data 并聚合结果 — reuse shared registry.

    中文：合成回退需显式 allow_synthetic=True（默认 True 保持存量离线可用，
    但回退仍标记 ok:False + provenance synthetic；显式 False 时直接抛错）。
    """
    data: Dict[str, Any] = {}
    reg = _get_shared_registry()
    for sym in symbols or []:
        try:
            bars, prov = reg.get_bars(sym, start, end, interval=interval)
            provenance = _provenance_dict(prov)
            data[sym] = {"bars": bars, "provenance": provenance, "ok": True}
        except Exception as e:
            from hero_quant.data.registry import CrossSourceError as _CSE

            if isinstance(e, _CSE):
                raise
            # 与 get_market_data 对齐 fail-closed：校验类与缺配置错误逐 symbol
            # 透传，不回退合成冒充数据
            if isinstance(e, (ValueError, TypeError)):
                raise
            if isinstance(e, ImportError):
                raise RuntimeError("market data misconfigured: no loader available") from e
            if not allow_synthetic:
                raise RuntimeError(
                    f"market data unavailable for {sym}: {e} (synthetic fallback requires allow_synthetic=True)"
                ) from e
            # 显式 opt-in 后按 symbol 回退合成，但标记 ok:False + provenance synthetic
            try:
                bars_fb = _synthetic_fallback(sym, start, end)
                data[sym] = {"bars": bars_fb, "provenance": {"source": "synthetic", "unit": "shares", "adjust": "none"}, "ok": False, "error": str(e)}
            except (OSError, RuntimeError, ValueError, TypeError) as e2:  # 中文：窄化捕获
                _logger.warning("synthetic fallback failed for %s: %s", sym, e2, exc_info=True)
                data[sym] = {"bars": [], "ok": False, "error": str(e2)}
    # 聚合顶层 ok：仅当全部 symbol ok 时才 True，否则 False（避免全回退仍 True 冒充 live）
    all_ok = bool(data) and all(v.get("ok", False) for v in data.values())
    return {"data": data, "ok": all_ok}
