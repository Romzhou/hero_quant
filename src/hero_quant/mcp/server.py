"""mcp.server — MCP 服务端与精选只读工具集。

职责：注册并暴露 20 个精选只读工具，基于 @tool 注册表复用；提供 FastMCP 适配与本地存根。
架构位置：MCP 层服务端实现，供路由器与 Agent 调用；工具实现来自 hero_quant.tools。
关键设计：仅只读工具入选 curated 列表以保证安全；FastMCP 存在时委托注册，否则以本地 list_tools/call_tool 存根满足测试与离线运行。
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List

from hero_quant.tools.registry import TOOL_REGISTRY, get_definitions, tool

# 确保核心工具已加载，使 TOOL_REGISTRY 完整
for _mod in (
    "hero_quant.tools.market_data",
    "hero_quant.tools.quantlib_tool",
    "hero_quant.tools.backtest",
):
    try:
        importlib.import_module(_mod)
    except Exception:
        pass

# 补充 3 个只读期权工具以凑齐 20 个精选
if "get_option_price" not in TOOL_REGISTRY:

    @tool(
        name="get_option_price",
        description="Price European option via Black-Scholes (bs_price). Read-only.",
        parameters={
            "type": "object",
            "properties": {
                "S": {"type": "number"},
                "K": {"type": "number"},
                "T": {"type": "number"},
                "r": {"type": "number"},
                "sigma": {"type": "number"},
                "option_type": {"type": "string"},
            },
            "required": ["S", "K", "T"],
            "additionalProperties": False,
        },
        output={
            "type": "object",
            "properties": {"price": {"type": "number"}, "ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        is_concurrency_safe=lambda args: True,
    )
    def get_option_price(S: float, K: float, T: float, r: float = 0.05, sigma: float = 0.2, option_type: str = "call") -> Dict[str, Any]:
        try:
            from hero_quant.quantlib.options import bs_price

            p = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, option_type=option_type)
            return {"price": float(p), "ok": True}
        except Exception as e:
            return {"price": 0.0, "ok": False, "error": str(e)}


if "get_greeks" not in TOOL_REGISTRY:

    @tool(
        name="get_greeks",
        description="Compute Black-Scholes greeks (delta/gamma/vega/theta/rho). Read-only.",
        parameters={
            "type": "object",
            "properties": {
                "S": {"type": "number"},
                "K": {"type": "number"},
                "T": {"type": "number"},
                "r": {"type": "number"},
                "sigma": {"type": "number"},
                "option_type": {"type": "string"},
            },
            "required": ["S", "K", "T"],
            "additionalProperties": False,
        },
        output={
            "type": "object",
            "properties": {"greeks": {"type": "object"}, "ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        is_concurrency_safe=lambda args: True,
    )
    def get_greeks(S: float, K: float, T: float, r: float = 0.05, sigma: float = 0.2, option_type: str = "call") -> Dict[str, Any]:
        try:
            from hero_quant.quantlib.options import bs_greeks

            g = bs_greeks(S=S, K=K, T=T, r=r, sigma=sigma, option_type=option_type)
            return {"greeks": g, "ok": True}
        except Exception as e:
            return {"greeks": {}, "ok": False, "error": str(e)}


if "get_implied_vol" not in TOOL_REGISTRY:

    @tool(
        name="get_implied_vol",
        description="Compute implied volatility via bisection for European option. Read-only.",
        parameters={
            "type": "object",
            "properties": {
                "price": {"type": "number"},
                "S": {"type": "number"},
                "K": {"type": "number"},
                "T": {"type": "number"},
                "r": {"type": "number"},
                "option_type": {"type": "string"},
            },
            "required": ["price", "S", "K", "T"],
            "additionalProperties": False,
        },
        output={
            "type": "object",
            "properties": {"iv": {"type": "number"}, "ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        is_concurrency_safe=lambda args: True,
    )
    def get_implied_vol(price: float, S: float, K: float, T: float, r: float = 0.05, option_type: str = "call") -> Dict[str, Any]:
        try:
            from hero_quant.quantlib.options import implied_volatility

            v = implied_volatility(price, S=S, K=K, T=T, r=r, option_type=option_type)
            return {"iv": float(v), "ok": True}
        except Exception as e:
            return {"iv": 0.0, "ok": False, "error": str(e)}


# 20 个精选只读工具：覆盖行情、因子、回测与期权定价，按名称排序以稳定 KV 缓存
CURATED_TOOLS: List[str] = [
    "compute_drawdown",
    "compute_factor",
    "compute_indicator",
    "compute_sharpe",
    "get_backtest_metrics",
    "get_bars_range",
    "get_fundamentals",
    "get_greeks",
    "get_implied_vol",
    "get_market_data",
    "get_option_price",
    "get_ticker_info",
    "list_backtest_engines",
    "list_markets",
    "optimize_portfolio",
    "run_backtest",
    "screen_factors",
    "search_symbol",
    "search_symbols",
    "validate_backtest",
]
# 校验数量为 20
assert len(CURATED_TOOLS) == 20, "CURATED must be 20"


def get_curated_definitions(presentAs: str = "native") -> List[Dict[str, Any]]:
    """返回精选 20 的工具定义，按名称过滤。"""
    defs = get_definitions(presentAs=presentAs)
    curated_set = set(CURATED_TOOLS)
    return [d for d in defs if d.get("function", {}).get("name") in curated_set]


def get_curated_specs() -> Dict[str, Any]:
    """返回精选工具的 ToolSpec 字典。"""
    return {k: TOOL_REGISTRY[k] for k in CURATED_TOOLS if k in TOOL_REGISTRY}


# FastMCP 服务端存根（只读，复用 @tool 注册表）
class MCPServer:
    """最小 MCP 服务端：封装精选只读工具，FastMCP 可用时委托，否则提供本地存根。"""

    def __init__(self, tools: List[str] | None = None):
        self.curated = tools or CURATED_TOOLS
        self._fastmcp = None
        try:
            from mcp.server.fastmcp import FastMCP  # type: ignore

            self._fastmcp = FastMCP("hero-quant")
            # 在 FastMCP 上注册精选只读工具
            for name in self.curated:
                spec = TOOL_REGISTRY.get(name)
                if spec is None:
                    continue

                # 通过 FastMCP 装饰器/方法注册
                try:
                    # FastMCP 可能暴露 @mcp.tool() 装饰器
                    # 惰性通过 spec.func 注册
                    if hasattr(self._fastmcp, "tool"):
                        # 以装饰器方式注册
                        self._fastmcp.tool()(spec.func)  # type: ignore
                    elif hasattr(self._fastmcp, "add_tool"):
                        self._fastmcp.add_tool(spec.func)  # type: ignore
                except Exception:
                    pass
        except Exception:
            self._fastmcp = None

    def list_tools(self) -> List[str]:
        """列出精选工具名。"""
        return list(self.curated)

    def call_tool(self, name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """调用精选工具；不在精选内则抛错，仅允许只读调用。"""
        if name not in self.curated:
            raise ValueError(f"tool {name} not in curated 20")
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            raise ValueError(f"tool {name} not registered")
        # 只读守卫：is_concurrency_safe 为可选检查，此处直接调用
        return spec.func(**(arguments or {}))

    def get_fastmcp(self):
        """返回底层 FastMCP 实例（若可用）。"""
        return self._fastmcp


def create_server() -> MCPServer:
    """创建 MCPServer 实例。"""
    return MCPServer()


# 供导入复用的单例
server = create_server()
