"""hero_quant.mcp — MCP 能力入口：精选只读工具与向量路由。

职责：对外暴露精选工具列表与 TopK 路由能力，供 Agent 选型调用。
架构位置：MCP 层门面，聚合 server（工具注册）与 router（BM25+向量混合召回）。
关键设计：仅暴露只读工具；路由以 BM25 为主、向量为辅，失败自动回退以保证可用性。
"""

from .server import CURATED_TOOLS
from .router import route

__all__ = ["CURATED_TOOLS", "route"]
