"""tools 包：工具注册与语义化合约的对外入口。"""

from .registry import TOOL_REGISTRY, ToolSpec, tool

__all__ = ["tool", "TOOL_REGISTRY", "ToolSpec"]
