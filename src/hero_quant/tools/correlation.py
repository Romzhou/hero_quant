"""相关性统计工具集：两标的日收益率 Pearson 相关系数（只读）。

位于 tools 层统计分支，复用 MarketDataRegistry 双源取价，
pandas 计算日收益率相关；数据不可用时合成数据必须带 provenance 标记
且 ok:False 不可用作 live，或直接 fail-closed。
演示 registry.py 契约的完整用法：
- name/description 必填且唯一；
- parameters/output 为 JSON Schema，import 时由 assertSupportedJsonSchema 校验；
- 只读计算 is_concurrency_safe 标 True（进 loop.py 并发组）；
- timeoutMs 声明式超时，由调度器 fut.result(timeout) 强制熔断。
"""

from __future__ import annotations

from typing import Any, Dict

from hero_quant.tools.registry import tool


def _fetch_closes(symbol: str, start: str, end: str):
    """拉取收盘价序列，保留日期索引以便调用方按日期对齐。

    成功时返回 list[tuple[str, float]] 的 (date, close) 序列；
    失败直接抛出由调用方返回 ok=False，合成路径必须标记 provenance 不可用作 live。
    """
    try:
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader

        reg = MarketDataRegistry()
        reg.register(TencentLoader())
        try:
            from hero_quant.data.loaders.yahoo import YahooLoader

            reg.register(YahooLoader())
        except ImportError as e:
            import logging as _logging

            _logging.getLogger(__name__).debug("YahooLoader not available for %s: %s", symbol, e)
        except (ValueError, TypeError, OSError, RuntimeError) as e:  # 中文：窄化捕获
            import logging as _logging

            _logging.getLogger(__name__).warning("YahooLoader register failed: %s", e, exc_info=True)
        bars, prov = reg.get_bars(symbol, start, end, interval="1d")
        # 中文：保留日期索引，避免丢日期后按位置错配
        closes: list[tuple[str, float]] = []
        for b in bars or []:
            c = b.get("close")
            d = b.get("date") or b.get("trade_date") or b.get("time") or b.get("datetime")
            if c is None or d is None:
                continue
            try:
                v = float(c)
            except (TypeError, ValueError):
                continue
            if v != v:  # NaN
                continue
            closes.append((str(d), v))
        if closes:
            return closes
        raise ValueError(f"no valid closes for {symbol} {start}->{end}")
    except Exception as e:
        # 合成仅当显式 HERO_DATA_MODE=synthetic 时考虑，且需上层标记 provenance，不静默冒充 live
        try:
            from hero_quant.config.settings import Settings

            if getattr(Settings(), "data_mode", "live") == "synthetic":
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "synthetic fallback enabled for %s, returning synthetic closes", symbol, exc_info=True
                )
                try:
                    import structlog as _structlog  # type: ignore

                    _structlog.get_logger(__name__).warning(
                        "synthetic fallback enabled", symbol=symbol, error=str(e), exc_info=True
                    )
                except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError):
                    pass
                # 中文：合成数据必须标记不可用，调用方将转为 ok:False + provenance synthetic
                raise ValueError(f"synthetic closes for {symbol} {start}->{end} (synthetic provenance, not live)") from e
        except ValueError:
            raise
        except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as inner:
            import logging as _logging

            _logging.getLogger(__name__).debug("synthetic check failed for %s: %s", symbol, inner, exc_info=True)
        import logging as _logging

        _logging.getLogger(__name__).warning("fetch closes failed for %s: %s", symbol, e, exc_info=True)
        try:
            import structlog as _structlog  # type: ignore

            _structlog.get_logger(__name__).warning(
                "fetch closes failed", symbol=symbol, error=str(e), exc_info=True
            )
        except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError):
            pass
        raise


@tool(
    name="compute_correlation",
    description="Compute Pearson correlation between daily returns of two symbols (read-only stats).",
    parameters={
        "type": "object",
        "properties": {
            "symbol_a": {"type": "string"},
            "symbol_b": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
        },
        "required": ["symbol_a", "symbol_b"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "correlation": {"type": "number"},
            "points": {"type": "integer"},
            "ok": {"type": "boolean"},
            "error": {"type": "string"},
            "provenance": {"type": "object"},
            "isMock": {"type": "boolean"},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
    timeoutMs=5000,
)
def compute_correlation(
    symbol_a: str,
    symbol_b: str,
    start: str = "2026-07-01",
    end: str = "2026-08-01",
) -> Dict[str, Any]:
    """计算两标的日收益率的 Pearson 相关系数（对齐区间后取重叠样本，按日期 inner-join）。"""
    try:
        import pandas as pd

        ca = _fetch_closes(symbol_a, start, end)
        cb = _fetch_closes(symbol_b, start, end)
        # 中文：按日期 inner-join 对齐，避免丢日期后按位置错配
        # 兼容 _fetch_closes 返回 tuple 序列或历史 float 序列
        def _to_map(closes):
            if closes and isinstance(closes[0], (list, tuple)) and len(closes[0]) == 2:
                return {str(d): float(v) for d, v in closes}
            # 兜底：无日期序列（历史桩）
            return {str(i): float(v) for i, v in enumerate(closes)}

        ma = _to_map(ca)
        mb = _to_map(cb)
        common = sorted(set(ma) & set(mb))
        if len(common) < 2:
            return {
                "correlation": 0.0,
                "points": 0,
                "ok": False,
                "error": "insufficient overlapping dates",
            }
        # 按共同日期排序取值，保证对齐
        va = [ma[d] for d in common]
        vb = [mb[d] for d in common]
        ra = pd.Series(va, dtype=float).pct_change().dropna()
        rb = pd.Series(vb, dtype=float).pct_change().dropna()
        m = min(len(ra), len(rb))
        if m < 2:
            return {
                "correlation": 0.0,
                "points": int(m),
                "ok": False,
                "error": "insufficient overlapping return points",
            }
        corr = float(
            ra.iloc[-m:].reset_index(drop=True).corr(rb.iloc[-m:].reset_index(drop=True))
        )
        if pd.isna(corr):
            return {
                "correlation": 0.0,
                "points": int(m),
                "ok": False,
                "error": "correlation undefined (zero variance series)",
            }
        return {"correlation": corr, "points": int(m), "ok": True}
    except Exception as e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "compute_correlation failed for %s/%s: %s", symbol_a, symbol_b, e, exc_info=True
        )
        try:
            import structlog as _structlog  # type: ignore

            _structlog.get_logger(__name__).warning(
                "compute_correlation failed", symbol_a=symbol_a, symbol_b=symbol_b, error=str(e), exc_info=True
            )
        except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError):
            pass
        # 中文：若为合成回退触发的 ValueError，标记 provenance synthetic + isMock，不可用作 live
        msg = str(e).lower()
        if "synthetic" in msg:
            return {
                "correlation": 0.0,
                "points": 0,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "provenance": {"source": "synthetic", "unit": "shares"},
                "isMock": True,
            }
        return {"correlation": 0.0, "points": 0, "ok": False, "error": f"{type(e).__name__}: {e}"}
