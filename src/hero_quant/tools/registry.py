from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict
import inspect


@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable
    signature: inspect.Signature | None = None


TOOL_REGISTRY: Dict[str, ToolSpec] = {}


def tool(name: str, description: str):
    """Decorator to register a function as a tool.

    Validates name uniqueness and non-empty description.
    Stores a ToolSpec in TOOL_REGISTRY and returns the original function.
    """
    if not description:
        raise ValueError("description must be non-empty")
    if name in TOOL_REGISTRY:
        raise ValueError(f"tool name '{name}' already registered")

    def decorator(func: Callable) -> Callable:
        if name in TOOL_REGISTRY:
            raise ValueError(f"tool name '{name}' already registered")
        if not description:
            raise ValueError("description must be non-empty")
        spec = ToolSpec(
            name=name,
            description=description,
            func=func,
            signature=inspect.signature(func),
        )
        TOOL_REGISTRY[name] = spec
        return func

    return decorator
