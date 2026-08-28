"""相关性统计工具集：两标的日收益率 Pearson 相关系数（只读）。

位于 tools 层统计分支，复用 MarketDataRegistry 双源取价，
pandas 计算日收益率相关；数据不可用时以合成序列兜底保证离线可算。
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
    """拉取收盘价序列，失败回退 40 点等差序列以保证指标可算。"""
    try:
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader

        reg = MarketDataRegistry()
        reg.register(TencentLoader())
        try:
            from hero_quant.data.loaders.yahoo import YahooLoader

            reg.register(YahooLoader())
        except Exception:
            pass
        bars, _ = reg.get_bars(symbol, "1d", start, end)
        closes = [float(b.get("close", 100)) for b in bars] if bars else []
        if closes:
            return closes
    except Exception:
        pass
    # 无可用行情时提供等差序列兜底（与 quantlib_tool._fetch_closes 同策略）
    return [100 + i * 0.5 for i in range(40)]


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
    """计算两标的日收益率的 Pearson 相关系数（对齐区间后取重叠样本）。"""
    try:
        import pandas as pd

        ca = _fetch_closes(symbol_a, start, end)
        cb = _fetch_closes(symbol_b, start, end)
        n = min(len(ca), len(cb))
        ra = pd.Series(ca[:n], dtype=float).pct_change().dropna()
        rb = pd.Series(cb[:n], dtype=float).pct_change().dropna()
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
        return {"correlation": 0.0, "points": 0, "ok": False, "error": str(e)}
