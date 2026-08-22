"""工具展示层桩：presentAs native/code/both 的形态分发。

位于 tools 层展示侧，当前保持 native 稳定输出以确保 KV-cache 命中；
code/both 为预留扩展，未来可接入统一渲染。
"""

from __future__ import annotations

from typing import Any, Dict, List


def present_as_native(spec: Any) -> Dict[str, Any]:
    """返回 OpenAI 兼容的 function 定义，兼容 ToolSpec 与 dict 输入。"""
    # Support both ToolSpec dataclass and dict
    name = getattr(spec, "name", spec.get("name") if isinstance(spec, dict) else "unknown")
    description = getattr(spec, "description", spec.get("description") if isinstance(spec, dict) else "")
    parameters = getattr(spec, "parameters", None)
    if parameters is None and isinstance(spec, dict):
        parameters = spec.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def present_as_code(spec: Any) -> str:
    """返回 code 解释器风格的注释式展示。"""
    name = getattr(spec, "name", spec.get("name") if isinstance(spec, dict) else "unknown")
    description = getattr(spec, "description", "")
    return f"# Tool: {name}\n# {description}\n"


def present(spec: Any, presentAs: str = "native") -> Any:
    """按 presentAs 分发单工具的展示形态。"""
    if presentAs == "native":
        return present_as_native(spec)
    if presentAs == "code":
        return present_as_code(spec)
    if presentAs == "both":
        return {
            "native": present_as_native(spec),
            "code": present_as_code(spec),
        }
    # fallback
    return present_as_native(spec)


def present_definitions(presentAs: str = "native") -> List[Dict[str, Any]]:
    """按请求形态返回全量工具定义（桩实现）。"""
    from .registry import TOOL_REGISTRY, get_definitions

    if presentAs == "native":
        return get_definitions()
    # code/both 形态逐个映射，复用 present() 的分发
    defs: List[Dict[str, Any]] = []
    for spec in TOOL_REGISTRY.values():
        defs.append(present(spec, presentAs=presentAs))
    return defs
